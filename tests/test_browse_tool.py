"""browse must not open the host's default browser (its history outlives eject)."""

import ui.browser
from agent import tools


def test_browse_uses_isolated_profile_in_session_dir(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ui.browser, "open_private",
                        lambda url, session_dir: calls.append((url, session_dir)) or "isolated")
    monkeypatch.setattr(ui.browser.webbrowser, "open",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("default browser")))
    monkeypatch.setenv("CARRY_AI_SESSION_DIR", str(tmp_path))

    out = tools._tool_browse("https://example.com")

    assert calls == [("https://example.com", str(tmp_path))]
    assert "isolated" in out
