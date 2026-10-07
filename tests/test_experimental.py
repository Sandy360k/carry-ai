"""
Peripheral features (godmode, onyx, google_workspace, cowork) must be OFF
by default and only load when experimental.<feature> is enabled.
"""

import pytest

from conftest import PROJECT_ROOT  # noqa: F401
from config.settings import is_experimental_enabled


FEATURES = ["godmode", "onyx", "google_workspace", "cowork"]


def test_all_experimental_features_default_off():
    for f in FEATURES:
        assert is_experimental_enabled(f) is False, f
    assert is_experimental_enabled("does-not-exist") is False


def test_load_providers_skips_experimental_by_default(monkeypatch):
    import modes.api_mode as api
    monkeypatch.setattr(api, "_instantiate_provider",
                        lambda name, keys: object())  # would load if reached
    provs = api.load_providers({"godmode": {"api_key": "x"},
                                "onyx": {"api_key": "y"},
                                "groq": {"api_key": "z"}})
    assert set(provs) == {"groq"}          # godmode/onyx skipped


def test_load_providers_includes_experimental_when_enabled(monkeypatch):
    import modes.api_mode as api
    monkeypatch.setattr(api, "_instantiate_provider",
                        lambda name, keys: object())
    monkeypatch.setattr("config.settings.is_experimental_enabled",
                        lambda feature, usb_root=None: feature == "godmode")
    provs = api.load_providers({"godmode": {"api_key": "x"},
                                "onyx": {"api_key": "y"}})
    assert set(provs) == {"godmode"}       # onyx still off


def test_cowork_route_absent_by_default():
    pytest.importorskip("flask")
    from ui.app import create_app
    app = create_app(config={"ui_token": "t", "mode": "api"})
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/cowork/sessions" not in rules


def test_cowork_route_present_when_enabled(monkeypatch):
    pytest.importorskip("flask")
    monkeypatch.setattr("config.settings.is_experimental_enabled",
                        lambda feature, usb_root=None: feature == "cowork")
    from ui.app import create_app
    app = create_app(config={"ui_token": "t", "mode": "api"})
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/cowork/sessions" in rules
