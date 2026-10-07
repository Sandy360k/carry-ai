"""
carry-ai/crypto/keystore.py -- Encrypted Key Storage
======================================================

Manages encrypted storage of API keys using Fernet symmetric encryption.
Keys are stored encrypted on the USB drive and only decrypted into RAM
during an active session. On cleanup, decrypted keys are zeroed out.

Encryption: Fernet (AES-128-CBC + HMAC-SHA256)
Key derivation: PBKDF2-HMAC-SHA256, 600k iterations, random 16-byte salt

Storage Format (providers.enc):
    {
        "salt": "<base64-encoded-16-byte-salt>",
        "data": "<Fernet-token (base64-encoded encrypted JSON)>"
    }

Decrypted data structure:
    {
        "anthropic": {"api_key": "sk-ant-..."},
        "openai": {"api_key": "sk-..."},
        "google": {"api_key": "...", "oauth_refresh_token": "..."},
        "groq": {"api_key": "gsk_..."},
        "openrouter": {"api_key": "sk-or-..."},
        "huggingface": {"token": "hf_..."}
    }
"""

import base64
import ctypes
import getpass
import json
import logging
import os
import sys

logger = logging.getLogger("carry-ai.crypto")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PBKDF2_ITERATIONS = 600_000   # OWASP 2024 recommendation
SALT_LENGTH = 16              # 128-bit random salt
DEFAULT_ENC_FILE = "config/providers.enc"

# Provider display names for interactive setup
KNOWN_PROVIDERS = {
    "anthropic":   {"fields": ["api_key"],   "prefix": "sk-ant-", "label": "Anthropic (Claude)"},
    "openai":      {"fields": ["api_key"],   "prefix": "sk-",     "label": "OpenAI"},
    "google":      {"fields": ["api_key"],   "prefix": "",        "label": "Google Gemini"},
    "groq":        {"fields": ["api_key"],   "prefix": "gsk_",    "label": "Groq"},
    "openrouter":  {"fields": ["api_key"],   "prefix": "sk-or-",  "label": "OpenRouter"},
    "huggingface": {"fields": ["token"],     "prefix": "hf_",     "label": "HuggingFace"},
}


# ---------------------------------------------------------------------------
# Cryptography helpers (lazy import to handle missing dependency)
# ---------------------------------------------------------------------------
def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a Fernet-compatible key from passphrase + salt."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    raw = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def _fernet(key: bytes):
    """Return a Fernet instance."""
    from cryptography.fernet import Fernet
    return Fernet(key)


# ---------------------------------------------------------------------------
# Secure memory wipe
# ---------------------------------------------------------------------------
def zero_secrets(obj):
    """
    Best-effort secure memory wipe for strings, bytes, and dicts.
    Uses ctypes.memset to overwrite CPython internal buffers.
    """
    if obj is None:
        return

    if isinstance(obj, dict):
        for v in obj.values():
            zero_secrets(v)
        obj.clear()
        return

    if isinstance(obj, list):
        for item in obj:
            zero_secrets(item)
        obj.clear()
        return

    # String or bytes: overwrite internal buffer on CPython
    try:
        if isinstance(obj, str):
            buf_addr = id(obj) + sys.getsizeof("") - 1
            buf_len = len(obj)
            if buf_len > 0:
                ctypes.memset(buf_addr, 0, buf_len)
        elif isinstance(obj, (bytes, bytearray)):
            buf_addr = id(obj) + sys.getsizeof(b"") - 1
            buf_len = len(obj)
            if buf_len > 0:
                ctypes.memset(buf_addr, 0, buf_len)
    except Exception:
        pass  # Non-CPython or protected memory


# ---------------------------------------------------------------------------
# KeyStore
# ---------------------------------------------------------------------------
class KeyStore:
    """
    Encrypted API key storage with secure memory handling.

    Usage:
        ks = KeyStore("config/providers.enc")

        # First-time setup
        ks.setup_interactive()

        # Load keys at boot
        ks.unlock("my-passphrase")
        key = ks.get("anthropic", "api_key")

        # Add/remove keys
        ks.add("openai", "api_key", "sk-...", save=True)
        ks.remove("openai", save=True)

        # Secure cleanup
        ks.lock()
    """

    def __init__(self, enc_path: str = None):
        """
        Args:
            enc_path: Path to encrypted providers file.
                      Defaults to config/providers.enc relative to project root.
        """
        if enc_path is None:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            enc_path = os.path.join(project_root, DEFAULT_ENC_FILE)

        self.enc_path = enc_path
        self._secrets: dict = {}       # Decrypted keys (only in RAM)
        self._passphrase: str = ""     # Cached passphrase (in RAM only)
        self._salt: bytes = b""        # Salt from enc file
        self._unlocked = False

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def unlock(self, passphrase: str) -> bool:
        """
        Decrypt the keystore file into RAM.

        Returns True if successful, False if file doesn't exist or is empty.
        Raises ValueError on wrong passphrase / corrupt data.
        """
        if not os.path.isfile(self.enc_path):
            logger.warning("Keystore file not found: %s", self.enc_path)
            return False

        try:
            with open(self.enc_path, "r", encoding="utf-8") as f:
                envelope = json.load(f)
        except (json.JSONDecodeError, KeyError):
            logger.warning("Keystore file is not valid JSON — treating as empty")
            return False

        # Check if it's the placeholder file
        if "salt" not in envelope or "data" not in envelope:
            logger.info("Keystore file has no encrypted data (placeholder)")
            return False

        self._salt = base64.b64decode(envelope["salt"])
        enc_data = envelope["data"].encode("utf-8")

        try:
            key = _derive_key(passphrase, self._salt)
            f = _fernet(key)
            decrypted = f.decrypt(enc_data)
            self._secrets = json.loads(decrypted.decode("utf-8"))
            self._passphrase = passphrase
            self._unlocked = True
            logger.info("Keystore unlocked. Providers: %s", list(self._secrets.keys()))
            return True
        except Exception as e:
            raise ValueError(f"Failed to decrypt keystore: {e}") from e

    def lock(self):
        """Securely zero all decrypted secrets from RAM."""
        if self._secrets:
            zero_secrets(self._secrets)
        self._secrets = {}
        # Overwrite passphrase buffer if it's a dynamic string (not interned)
        # then discard the reference. Avoid memset on potentially-interned literals.
        try:
            if self._passphrase and len(self._passphrase) > 0:
                # Create a bytearray copy, zero it, then drop both references
                buf = bytearray(self._passphrase.encode("utf-8"))
                ctypes.memset((ctypes.c_char * len(buf)).from_buffer(buf), 0, len(buf))
                del buf
        except Exception:
            pass
        self._passphrase = ""
        self._unlocked = False
        logger.info("Keystore locked -- secrets zeroed from memory")

    def is_unlocked(self) -> bool:
        return self._unlocked

    # ------------------------------------------------------------------
    # Key access
    # ------------------------------------------------------------------

    def get(self, provider: str, field: str = "api_key") -> str | None:
        """
        Retrieve a decrypted key value.

        Args:
            provider: Provider name (e.g., "anthropic", "openai")
            field: Field name (e.g., "api_key", "token")

        Returns the key value or None if not found.
        """
        if not self._unlocked:
            logger.warning("Keystore is locked — call unlock() first")
            return None
        prov_data = self._secrets.get(provider, {})
        return prov_data.get(field)

    def get_provider_keys(self, provider: str) -> dict:
        """Get all keys for a provider. Returns empty dict if not found."""
        if not self._unlocked:
            return {}
        return dict(self._secrets.get(provider, {}))

    def decrypt_interactive(self, max_attempts: int = 3) -> dict:
        """Prompt for the passphrase, unlock, and return ``{provider: api_key}``.

        Used at boot by launcher.py and the desktop app. Returns an empty
        dict if the keystore file is missing/empty. Raises ValueError if the
        passphrase is wrong after *max_attempts* tries.
        """
        if not os.path.isfile(self.enc_path):
            return {}
        content = ""
        try:
            with open(self.enc_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except OSError:
            return {}
        if not content or content.startswith("#"):
            return {}  # placeholder file, no real keys yet

        for attempt in range(1, max_attempts + 1):
            passphrase = getpass.getpass("  Keystore passphrase: ")
            try:
                if self.unlock(passphrase):
                    break
                return {}
            except ValueError:
                remaining = max_attempts - attempt
                if remaining:
                    print(f"  Wrong passphrase — {remaining} attempt(s) left.")
                else:
                    raise

        # Flatten to {provider: api_key} — what the boot context and
        # providers expect. Providers needing extra fields read the keystore.
        flat: dict[str, str] = {}
        for provider in self.list_providers():
            key = self.get(provider, "api_key")
            if key:
                flat[provider] = key
        return flat

    def list_providers(self) -> list:
        """List provider names that have stored keys."""
        if not self._unlocked:
            return []
        return list(self._secrets.keys())

    def has_provider(self, provider: str) -> bool:
        """Check if a provider has stored keys."""
        return provider in self._secrets

    # ------------------------------------------------------------------
    # Key mutation
    # ------------------------------------------------------------------

    def add(self, provider: str, field: str, value: str, save: bool = True):
        """
        Add or update a key.

        Args:
            provider: Provider name
            field: Key field name (e.g., "api_key")
            value: Key value
            save: If True, re-encrypt and write to disk immediately
        """
        if not self._unlocked and not self._passphrase:
            raise RuntimeError("Keystore not unlocked — set a passphrase first")

        if provider not in self._secrets:
            self._secrets[provider] = {}
        self._secrets[provider][field] = value
        logger.info("Added key: %s.%s", provider, field)

        if save:
            self._save()

    def remove(self, provider: str, save: bool = True) -> bool:
        """
        Remove all keys for a provider.

        Returns True if the provider existed.
        """
        if provider in self._secrets:
            zero_secrets(self._secrets[provider])
            del self._secrets[provider]
            logger.info("Removed provider: %s", provider)
            if save:
                self._save()
            return True
        return False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self):
        """Write pending changes (after add/remove with save=False)."""
        self._save()

    def _save(self):
        """Encrypt current secrets and write to enc_path."""
        if not self._passphrase:
            raise RuntimeError("No passphrase — cannot save")

        # Generate new salt each save for forward secrecy
        self._salt = os.urandom(SALT_LENGTH)
        key = _derive_key(self._passphrase, self._salt)
        f = _fernet(key)

        plaintext = json.dumps(self._secrets, separators=(",", ":")).encode("utf-8")
        encrypted = f.encrypt(plaintext)

        envelope = {
            "salt": base64.b64encode(self._salt).decode("ascii"),
            "data": encrypted.decode("ascii"),
        }

        os.makedirs(os.path.dirname(self.enc_path), exist_ok=True)
        with open(self.enc_path, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, indent=2)

        logger.info("Keystore saved to %s", self.enc_path)

    def init_new(self, passphrase: str):
        """
        Initialize a new empty keystore with the given passphrase.
        Use this for first-time setup before adding keys.
        """
        self._passphrase = passphrase
        self._secrets = {}
        self._unlocked = True
        self._save()
        logger.info("New keystore initialized")

    # ------------------------------------------------------------------
    # Interactive setup (CLI)
    # ------------------------------------------------------------------

    def setup_interactive(self):
        """
        Interactive wizard for first-time key entry.
        Prompts for passphrase and API keys via terminal.
        """
        print("\n  carry-ai Key Setup Wizard")
        print("  " + "=" * 30)
        print()

        # Passphrase
        if os.path.isfile(self.enc_path):
            print("  Existing keystore found. Enter passphrase to modify.")
            passphrase = getpass.getpass("  Passphrase: ")
            try:
                self.unlock(passphrase)
                print(f"  Unlocked. Current providers: {', '.join(self.list_providers()) or 'none'}")
            except ValueError:
                print("  Wrong passphrase!")
                return
        else:
            print("  No keystore found. Creating new one.")
            passphrase = getpass.getpass("  Set a passphrase: ")
            confirm = getpass.getpass("  Confirm passphrase: ")
            if passphrase != confirm:
                print("  Passphrases don't match!")
                return
            if len(passphrase) < 4:
                print("  Passphrase too short (min 4 chars)")
                return
            self.init_new(passphrase)

        print()

        # Walk through known providers
        for name, info in KNOWN_PROVIDERS.items():
            existing = self.has_provider(name)
            marker = " [configured]" if existing else ""
            resp = input(f"  Configure {info['label']}{marker}? [y/N] ").strip().lower()
            if resp != "y":
                continue

            for field in info["fields"]:
                current = self.get(name, field)
                hint = f" (current: ...{current[-6:]})" if current else ""
                value = getpass.getpass(f"    {field}{hint}: ").strip()
                if value:
                    # Basic prefix validation
                    if info["prefix"] and not value.startswith(info["prefix"]):
                        print(f"    Warning: expected prefix '{info['prefix']}' - saving anyway")
                    self.add(name, field, value, save=False)
                elif not current:
                    print(f"    Skipped {field}")

        self._save()

        print(f"\n  Done! Providers configured: {', '.join(self.list_providers()) or 'none'}")
        print(f"  Encrypted keystore: {self.enc_path}")
        print()


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="carry-ai-keystore",
        description="Manage encrypted API keys for carry-ai.",
    )
    sub = parser.add_subparsers(dest="command")

    # setup
    sub.add_parser("setup", help="Interactive key setup wizard")

    # list
    sub.add_parser("list", help="List configured providers")

    # add
    p_add = sub.add_parser("add", help="Add a single key")
    p_add.add_argument("provider", help="Provider name (e.g., anthropic)")
    p_add.add_argument("field", nargs="?", default="api_key", help="Field name (default: api_key)")

    # remove
    p_rem = sub.add_parser("remove", help="Remove a provider's keys")
    p_rem.add_argument("provider", help="Provider name")

    args = parser.parse_args()

    # Resolve enc path
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    enc_path = os.path.join(project_root, DEFAULT_ENC_FILE)
    ks = KeyStore(enc_path)

    if args.command == "setup":
        ks.setup_interactive()

    elif args.command == "list":
        passphrase = getpass.getpass("Passphrase: ")
        try:
            if ks.unlock(passphrase):
                providers = ks.list_providers()
                if providers:
                    for p in providers:
                        keys = ks.get_provider_keys(p)
                        fields = ", ".join(keys.keys())
                        print(f"  {p}: {fields}")
                else:
                    print("  No providers configured.")
            else:
                print("  Keystore file not found or empty.")
        except ValueError as e:
            print(f"  Error: {e}")
        finally:
            ks.lock()

    elif args.command == "add":
        passphrase = getpass.getpass("Passphrase: ")
        try:
            if not ks.unlock(passphrase):
                print("  No keystore found. Run 'setup' first.")
                return
            value = getpass.getpass(f"  Enter {args.provider}.{args.field}: ")
            if value:
                ks.add(args.provider, args.field, value)
                print(f"  Added {args.provider}.{args.field}")
            else:
                print("  Empty value — skipped.")
        except ValueError as e:
            print(f"  Error: {e}")
        finally:
            ks.lock()

    elif args.command == "remove":
        passphrase = getpass.getpass("Passphrase: ")
        try:
            if not ks.unlock(passphrase):
                print("  No keystore found.")
                return
            if ks.remove(args.provider):
                print(f"  Removed {args.provider}")
            else:
                print(f"  Provider '{args.provider}' not found")
        except ValueError as e:
            print(f"  Error: {e}")
        finally:
            ks.lock()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
