"""
Provider tests — model discovery, fallbacks and request-parameter rules.
No network: the providers' HTTP sessions are replaced with fakes.
"""

from datetime import date

import pytest

pytest.importorskip("requests")

from providers.base import clear_model_cache


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Stand-in for requests.Session: canned GET, records POST payloads."""

    def __init__(self, get_payload: dict | None = None, get_error: Exception | None = None):
        self.get_payload = get_payload
        self.get_error = get_error
        self.get_calls = 0
        self.posted: list[dict] = []

    def get(self, url, **kwargs):
        self.get_calls += 1
        if self.get_error:
            raise self.get_error
        return FakeResponse(self.get_payload or {})

    def post(self, url, json=None, **kwargs):
        self.posted.append(json)
        return FakeResponse({"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                             "content": [{"type": "text", "text": "ok"}]})

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_model_cache()
    yield
    clear_model_cache()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module, cls, key", [
    ("providers.openai_provider", "OpenAIProvider", "sk-test"),
    ("providers.groq_provider", "GroqProvider", "gsk_test"),
    ("providers.openrouter_provider", "OpenRouterProvider", "sk-or-test"),
    ("providers.anthropic_provider", "AnthropicProvider", "sk-ant-test-key-123456"),
    ("providers.google_oauth", "GoogleProvider", "AIza-test-key"),
])
def test_discovery_falls_back_to_hardcoded_list_on_error(module, cls, key):
    import importlib
    mod = importlib.import_module(module)
    prov = getattr(mod, cls)(api_key=key)
    prov._session = FakeSession(get_error=ConnectionError("offline"))

    assert prov.models() == list(mod.AVAILABLE_MODELS)
    # Unknown model passes through when the live list is unknown
    assert prov.resolve_model("some-new-model") == "some-new-model"
    # Failure is cached: no second 5 s stall
    prov.models()
    assert prov._session.get_calls == 1


def test_discovery_result_is_cached_and_filtered():
    from providers.groq_provider import GroqProvider
    prov = GroqProvider(api_key="gsk_test")
    prov._session = FakeSession(get_payload={"data": [
        {"id": "openai/gpt-oss-20b"}, {"id": "whisper-large-v3"},
        {"id": "meta-llama/llama-guard-4-12b"}, {"id": "openai/gpt-oss-120b"},
    ]})
    assert prov.models() == ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    prov.models()
    assert prov._session.get_calls == 1


def test_dead_configured_model_falls_back_to_default():
    from providers.groq_provider import GroqProvider, DEFAULT_MODEL
    prov = GroqProvider(api_key="gsk_test")
    prov._session = FakeSession(get_payload={"data": [
        {"id": "openai/gpt-oss-20b"}, {"id": "openai/gpt-oss-120b"},
    ]})
    prov.chat([{"role": "user", "content": "hi"}], model="llama-3.3-70b-versatile")
    assert prov._session.posted[-1]["model"] == DEFAULT_MODEL


def test_openrouter_filter_drops_expired_and_batch():
    from providers.openrouter_provider import filter_openrouter_models
    today = date(2026, 9, 26)
    records = [
        {"id": "openrouter/free"},
        {"id": "google/gemma-4-31b-it:free", "expiration_date": None},
        {"id": "openai/gpt-6-luna:batch"},
        {"id": "old/model-expired", "expiration_date": "2026-09-01"},
        {"id": "old/model-soon", "expiration_date": "2026-10-05"},
        {"id": "ok/model-later", "expiration_date": "2027-01-01"},
        {"id": "img/only", "architecture": {"output_modalities": ["image"]}},
    ]
    assert filter_openrouter_models(records, today=today) == [
        "openrouter/free", "google/gemma-4-31b-it:free", "ok/model-later",
    ]


# ---------------------------------------------------------------------------
# Request-parameter rules
# ---------------------------------------------------------------------------

def _offline(prov):
    prov._session = FakeSession(get_error=ConnectionError("offline"))
    return prov


def test_openai_reasoning_model_params():
    from providers.openai_provider import OpenAIProvider
    prov = _offline(OpenAIProvider(api_key="sk-test"))
    payload = prov._build_payload([{"role": "user", "content": "hi"}],
                                  model="gpt-6-sol", temperature=0.2,
                                  top_p=0.9, max_tokens=256)
    assert "temperature" not in payload and "top_p" not in payload
    assert "max_tokens" not in payload
    assert payload["max_completion_tokens"] == 256


def test_openai_tool_rules():
    from providers.openai_provider import OpenAIProvider
    prov = _offline(OpenAIProvider(api_key="sk-test"))
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    msgs = [{"role": "user", "content": "hi"}]

    luna = prov._build_payload(msgs, model="gpt-6-luna", tools=tools)
    assert luna["reasoning_effort"] == "none"

    astra = prov._build_payload(msgs, model="gpt-6-astra", tools=tools)
    assert astra["model"] == "gpt-6-luna" and astra["reasoning_effort"] == "none"

    astra_plain = prov._build_payload(msgs, model="gpt-6-astra")
    assert astra_plain["model"] == "gpt-6-astra"
    assert "reasoning_effort" not in astra_plain


def test_anthropic_new_models_get_no_sampling_params():
    from providers.anthropic_provider import AnthropicProvider
    prov = _offline(AnthropicProvider(api_key="sk-ant-test-key-123456"))
    msgs = [{"role": "user", "content": "hi"}]
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5-1"):
        payload = prov._build_payload(msgs, model=model, temperature=0.3, top_p=0.9)
        assert "temperature" not in payload and "top_p" not in payload

    haiku = prov._build_payload(msgs, model="claude-haiku-4-5", temperature=0.3)
    assert haiku["temperature"] == 0.3


def test_anthropic_refusal_is_surfaced():
    from providers.anthropic_provider import AnthropicProvider, REFUSAL_MESSAGE
    prov = _offline(AnthropicProvider(api_key="sk-ant-test-key-123456"))
    resp = prov._parse_response(
        {"stop_reason": "refusal", "content": [{"type": "text", "text": "partial"}]},
        "claude-opus-5")
    assert resp.content == REFUSAL_MESSAGE and resp.tool_calls is None


def test_gemini3_gets_no_temperature():
    from providers.google_oauth import GoogleProvider
    prov = _offline(GoogleProvider(api_key="AIza-test-key"))
    payload = prov._build_payload([{"role": "user", "content": "hi"}],
                                  model="gemini-3.5-flash-lite", temperature=0.2)
    assert "temperature" not in payload.get("generationConfig", {})


def test_router_uses_per_provider_model_on_fallback():
    from modes.api_mode import APIRouter
    from providers.base import BaseProvider, ChatResponse, ProviderError

    class Stub(BaseProvider):
        def __init__(self, name, fail):
            self._name, self._fail, self.seen = name, fail, []

        @property
        def provider_name(self):
            return self._name

        def chat(self, messages, **kwargs):
            self.seen.append(kwargs.get("model"))
            if self._fail:
                raise ProviderError("boom", provider=self._name)
            return ChatResponse(content="ok", provider=self._name)

        def stream(self, messages, **kwargs):
            yield {"type": "done", "data": None}

        def is_available(self):
            return True

        def models(self):
            return []

    router = APIRouter(provider_models={"b": "model-b"})
    a, b = Stub("a", fail=True), Stub("b", fail=False)
    router._providers = {"a": a, "b": b}
    from modes.api_mode import ProviderHealth
    router._health = {n: ProviderHealth(provider_name=n) for n in router._providers}
    router._started = True
    router._primary_provider_name = "a"

    resp = router.chat([{"role": "user", "content": "hi"}], model="model-a")
    assert resp.provider == "b"
    assert a.seen and set(a.seen) == {"model-a"}
    assert b.seen == ["model-b"]
