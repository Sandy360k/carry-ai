"""
carry-ai/models/hf_auth.py — Hugging Face sign-in and gated-repo checks
========================================================================

Lets the user get a Hugging Face token without opening a browser on the
host PC, and tells them up front when a model is gated.

Sign-in uses OAuth 2.0 Device Authorization (RFC 8628), which Hugging Face
supports for public apps (client id only, no secret):

    1. POST /oauth/device  (client_id, scope)   -> user_code + verification_uri
    2. The user opens the URL on ANY device (their phone) and enters the code.
    3. POST /oauth/token   (device_code grant)  -> access_token
       polled every `interval` s; "authorization_pending" until approved,
       "slow_down" adds 5 s, "expired_token"/"access_denied" end the flow.

Only the ``gated-repos`` scope is requested (read public gated repos the
user has been granted), plus ``openid profile`` to show who signed in.

Gated licences cannot be accepted through the API — Hugging Face requires
the user to click "Agree" on the model page ("Requesting access can only
be done from your browser"). ``check_access`` detects that case so the UI
can show the model page (as a QR code for the user's phone) and re-check.

The OAuth app (a *public* app, no secret, scope ``gated-repos``) is
registered once at https://huggingface.co/settings/applications/new and
its client id set in settings ``huggingface.oauth_client_id``.

Stdlib only: also imported by flash_usb.py on a bare Python.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

log = logging.getLogger("carry-ai.models.hf_auth")

HF_BASE = "https://huggingface.co"
DEVICE_URL = f"{HF_BASE}/oauth/device"
TOKEN_URL = f"{HF_BASE}/oauth/token"
WHOAMI_URL = f"{HF_BASE}/api/whoami-v2"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_SCOPE = "openid profile gated-repos"
_UA = {"User-Agent": "carry-ai/hf-auth"}
_TIMEOUT = 15


class HfAuthError(RuntimeError):
    """Sign-in failed, was denied, or expired."""


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int = 900
    interval: int = 5
    verification_uri_complete: str = ""

    @property
    def link(self) -> str:
        """URL to show / encode as QR: pre-filled with the code if offered."""
        return self.verification_uri_complete or self.verification_uri


@dataclass
class HfToken:
    access_token: str
    expires_in: int | None = None
    refresh_token: str | None = None
    scope: str = ""


def configured_client_id() -> str:
    """OAuth client id from settings (empty if not set up yet)."""
    try:
        from config.settings import load_settings
        return (load_settings().to_dict().get("huggingface", {})
                .get("oauth_client_id", "") or "").strip()
    except Exception as e:
        log.debug("Cannot read huggingface settings: %s", e)
        return ""


def _post_form(url: str, fields: dict) -> dict:
    body = urlencode(fields).encode("ascii")
    req = Request(url, data=body, headers={
        **_UA, "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"})
    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as e:
        # OAuth errors (authorization_pending etc.) come back as 400 + JSON
        try:
            return json.loads(e.read().decode("utf-8") or "{}")
        except (ValueError, OSError):
            raise HfAuthError(f"HTTP {e.code} from {url}") from e


def start_device_flow(client_id: str, scope: str = DEFAULT_SCOPE) -> DeviceCode:
    """Step 1: get a user code to show and a device code to poll with."""
    if not client_id:
        raise HfAuthError("No Hugging Face OAuth client id configured "
                          "(settings huggingface.oauth_client_id).")
    try:
        data = _post_form(DEVICE_URL, {"client_id": client_id, "scope": scope})
    except URLError as e:
        raise HfAuthError(f"Cannot reach huggingface.co: {e.reason}") from e
    if "device_code" not in data:
        raise HfAuthError(data.get("error_description") or data.get("error")
                          or "Unexpected response from Hugging Face")
    return DeviceCode(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data.get("verification_uri", DEVICE_URL),
        verification_uri_complete=data.get("verification_uri_complete", ""),
        expires_in=int(data.get("expires_in", 900)),
        interval=int(data.get("interval", 5)),
    )


def poll_for_token(client_id: str, code: DeviceCode,
                   cancel: threading.Event | None = None,
                   sleep=time.sleep) -> HfToken:
    """Step 3: poll until the user approves on their phone.

    Raises HfAuthError on denial, expiry or cancel.
    """
    interval = max(1, code.interval)
    deadline = time.monotonic() + code.expires_in
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise HfAuthError("Sign-in cancelled.")
        try:
            data = _post_form(TOKEN_URL, {
                "grant_type": DEVICE_GRANT,
                "device_code": code.device_code,
                "client_id": client_id,
            })
        except URLError as e:
            log.debug("Token poll network error (will retry): %s", e)
            data = {"error": "authorization_pending"}

        if data.get("access_token"):
            return HfToken(
                access_token=data["access_token"],
                expires_in=data.get("expires_in"),
                refresh_token=data.get("refresh_token"),
                scope=data.get("scope", ""),
            )
        error = data.get("error", "")
        if error == "slow_down":
            interval += 5
        elif error == "access_denied":
            raise HfAuthError("Sign-in was declined on Hugging Face.")
        elif error == "expired_token":
            break
        elif error and error != "authorization_pending":
            raise HfAuthError(data.get("error_description") or error)
        sleep(interval)
    raise HfAuthError("The sign-in code expired. Start again.")


def whoami(token: str) -> str:
    """Username for a token ('' if it can't be read)."""
    req = Request(WHOAMI_URL, headers={**_UA, "Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")).get("name", "")
    except (HTTPError, URLError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Gated repos
# ---------------------------------------------------------------------------

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # keep the 302, don't follow
        return None


def repo_page_url(repo_id: str) -> str:
    return f"{HF_BASE}/{repo_id}"


def gating_mode(repo_id: str, token: str | None = None) -> str:
    """'' if not gated, else 'auto' or 'manual' (author approval)."""
    req = Request(f"{HF_BASE}/api/models/{repo_id}",
                  headers={**_UA, **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:
            gated = json.loads(resp.read().decode("utf-8")).get("gated", False)
    except (HTTPError, URLError, ValueError):
        return ""
    return "" if not gated else str(gated)


def check_access(repo_id: str, filename: str, token: str | None = None) -> str:
    """Can this file be downloaded with this token?

    Returns:
        "ok"            downloadable now
        "auth_required" gated and no valid token (missing/expired) — sign in
        "no_access"     signed in, but the licence isn't accepted/approved yet
        "not_found"     repo or file doesn't exist
        "unknown"       network problem; let the download try
    """
    url = f"{HF_BASE}/{repo_id}/resolve/main/{quote(filename)}"
    headers = dict(_UA)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers, method="HEAD")
    try:
        with build_opener(_NoRedirect).open(req, timeout=_TIMEOUT):
            return "ok"
    except HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return "ok"                     # redirect to the CDN = allowed
        # HF answers 401 (X-Error-Code: GatedRepo) both with no token and
        # with an invalid/expired one -> sign in. A valid token whose user
        # hasn't been granted the licence gets 403.
        if e.code == 401:
            return "auth_required"
        if e.code == 403:
            return "no_access"
        if e.code == 404:
            return "not_found"
        return "unknown"
    except URLError:
        return "unknown"
