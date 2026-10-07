"""
Cloud provider catalogue (providers/catalog.py): key clean-up, key checks
(HTTP faked), the generic OpenAI-compatible provider, and the agent picking
up keys added mid-session.
"""

import io
from urllib.error import HTTPError, URLError

import pytest

from conftest import PROJECT_ROOT  # noqa: F401
from providers import catalog as c


def _http_error(code, body=b""):
    return HTTPError("https://x", code, "err", {}, io.BytesIO(body))


def _opener(result):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def open_(req, timeout=None):
        seen["req"] = req
        if isinstance(result, Exception):
            raise result
        return _Resp()
    return open_, seen


def test_catalog_entries_are_complete():
    keys = [p.key for p in c.PROVIDERS]
    assert len(keys) == len(set(keys))
    assert c.free_providers() and c.paid_providers()
    assert c.free_providers()[0].key == "openrouter"
    from modes.api_mode import PROVIDER_REGISTRY
    for p in c.PROVIDERS:
        assert p.key_url.startswith("https://")
        assert p.default_model
        # Served by its own module or the generic OpenAI-compatible one
        assert p.key in PROVIDER_REGISTRY or p.base_url.startswith("https://")


@pytest.mark.parametrize("raw", [
    "sk-or-abc", "  sk-or-abc \n", '"sk-or-abc"', "export OPENROUTER_API_KEY=sk-or-abc",
    "export OPENROUTER_API_KEY='sk-or-abc';", "set OPENROUTER_API_KEY=sk-or-abc",
])
def test_normalize_key_cleans_pastes(raw):
    assert c.normalize_key(raw) == "sk-or-abc"


def test_detected_key_reads_env_var():
    assert c.detected_key("groq", {"GROQ_API_KEY": " gsk_x "}) == "gsk_x"
    assert c.detected_key("groq", {}) == ""
    assert c.detected_key("nope", {"GROQ_API_KEY": "gsk_x"}) == ""


@pytest.mark.parametrize("result,state", [
    (None, "ok"),
    (_http_error(401), "invalid"),
    (_http_error(403), "invalid"),
    (_http_error(400, b'{"error":{"message":"API key not valid"}}'), "invalid"),
    (_http_error(400, b'{"error":"bad request"}'), "error"),
    (_http_error(429), "limited"),
    (_http_error(402), "limited"),
    (_http_error(500), "error"),
    (URLError("offline"), "error"),
])
def test_validate_key_maps_responses(result, state):
    opener, _ = _opener(result)
    assert c.validate_key("groq", "gsk_x", opener=opener)[0] == state


def test_validate_key_probes():
    opener, seen = _opener(None)
    c.validate_key("groq", "gsk_x", opener=opener)
    assert seen["req"].full_url == "https://api.groq.com/openai/v1/models"
    assert seen["req"].get_header("Authorization") == "Bearer gsk_x"

    # OpenRouter's /models is public; /key needs a valid key
    c.validate_key("openrouter", "sk-or-x", opener=opener)
    assert seen["req"].full_url == "https://openrouter.ai/api/v1/key"

    c.validate_key("anthropic", "sk-ant-x", opener=opener)
    assert seen["req"].full_url == "https://api.anthropic.com/v1/models"
    assert seen["req"].get_header("X-api-key") == "sk-ant-x"

    # NVIDIA's /models is public too: smallest real chat call
    c.validate_key("nvidia", "nvapi-x", opener=opener)
    assert seen["req"].full_url.endswith("/chat/completions")
    assert seen["req"].get_method() == "POST"


def test_validate_key_rejects_empty_and_unknown():
    assert c.validate_key("groq", "  ")[0] == "invalid"
    assert c.validate_key("nope", "k")[0] == "error"


def test_generic_provider_from_catalog():
    pytest.importorskip("requests")
    from modes.api_mode import _instantiate_provider
    prov = _instantiate_provider("cerebras", {"api_key": "csk-x"})
    assert prov.provider_name == "cerebras"
    assert prov._base_url == "https://api.cerebras.ai/v1"
    assert prov.default_model == "gpt-oss-120b"
    assert prov._headers()["Authorization"] == "Bearer csk-x"
    assert _instantiate_provider("not-a-provider", {"api_key": "x"}) is None


def test_load_providers_skips_non_chat_credentials():
    pytest.importorskip("requests")
    from modes.api_mode import load_providers
    loaded = load_providers({"huggingface": {"api_key": "hf_x"},
                             "mistral": {"api_key": "m-x"}})
    assert list(loaded) == ["mistral"]


def test_agent_set_api_keys_switches_local_session(tmp_path):
    from agent.agent import Agent
    a = Agent(boot_context={"mode": "local", "model_path": str(tmp_path / "m.gguf"),
                            "memory_path": str(tmp_path / "mem.db")})
    assert a._uses_local(None)

    a.set_api_keys({"huggingface": {"api_key": "hf_x"}})   # not a chat provider
    assert a._mode == "local"

    a.set_api_keys({"groq": {"api_key": "gsk_x"}})
    assert a._mode == "hybrid"
    assert a._uses_local("local") and not a._uses_local("groq")
    assert a._context["api_keys"] == {"groq": {"api_key": "gsk_x"}}


def test_agent_set_api_keys_without_model_goes_api(tmp_path):
    from agent.agent import Agent
    a = Agent(boot_context={"mode": "local", "memory_path": str(tmp_path / "mem.db")})
    a.set_api_keys({"openrouter": {"api_key": "sk-or-x"}})
    assert a._mode == "api" and not a._uses_local("local")
