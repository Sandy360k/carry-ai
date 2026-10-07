"""
run_python sandbox (integrations/python_sandbox.py, pydantic-monty).

The logic is tested against a fake Monty so CI needs no binary; the last
test runs the real sandbox when pydantic-monty is installed.
"""

import types
from pathlib import Path

import pytest

from conftest import PROJECT_ROOT  # noqa: F401
import integrations.python_sandbox as ps


# ---------------------------------------------------------------------------
# Fake pydantic_monty
# ---------------------------------------------------------------------------

class MontyError(Exception):
    def display(self, format="traceback"):
        return f"Traceback:\n{self.args[0]}"


class MontyCrashedError(MontyError):
    pass


def _fake_monty(results):
    """results: list of (printed, value_or_exception) per feed_run."""
    log = {"sessions": 0, "closed": 0, "feeds": []}

    class CollectString:
        def __init__(self, max_bytes=None):
            self.output = ""

    class Session:
        def __enter__(self):
            log["sessions"] += 1
            return self

        def __exit__(self, *a):
            log["closed"] += 1

        def feed_run(self, code, print_callback=None, mount=None, cwd=None):
            log["feeds"].append((code, cwd))
            printed, value = results.pop(0)
            print_callback.output = printed
            if isinstance(value, Exception):
                raise value
            return value

    class Monty:
        def __init__(self, **kw):
            log["pool"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def checkout(self, **kw):
            log["limits"] = kw["limits"]
            return Session()

    class MountDir:
        def __init__(self, **kw):
            log["mount"] = kw

        def close(self):
            pass

    mod = types.SimpleNamespace(Monty=Monty, MountDir=MountDir, CollectString=CollectString,
                                MontyError=MontyError, MontyCrashedError=MontyCrashedError,
                                __file__=__file__)
    return mod, log


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    def make(results, **kw):
        mod, log = _fake_monty(results)
        monkeypatch.setattr(ps, "_monty", mod)
        return ps.PythonSandbox(work_dir=tmp_path, binary_path="/x/monty", **kw), log
    return make


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------

def test_output_and_value_are_returned(sandbox, tmp_path):
    sb, log = sandbox([("hi\n", 42), ("", None)], timeout_s=5, max_memory_mb=64)
    assert sb.run("print('hi'); 42") == "hi\n→ 42"
    assert sb.run("x = 1") == "(no output)"
    assert log["sessions"] == 1                       # state kept between runs
    assert log["limits"] == {"max_feed_duration_secs": 5.0, "max_memory": 64 * 1024 * 1024}
    assert log["mount"]["host_path"] == tmp_path and log["mount"]["mode"] == "read-write"
    assert log["feeds"][0][1] == "/work"


def test_errors_show_traceback_and_keep_state(sandbox):
    sb, log = sandbox([("", MontyError("NameError: x")), ("", 1)])
    assert "NameError" in sb.run("x")
    sb.run("1")
    assert log["sessions"] == 1


@pytest.mark.parametrize("err", ["TimeoutError: feed time limit", "MemoryError: limit"])
def test_limits_reset_the_session(sandbox, err):
    sb, log = sandbox([("", MontyError(err)), ("", 1)])
    assert "Variables were reset" in sb.run("loop")
    sb.run("1")
    assert log["sessions"] == 2


def test_crash_resets_the_session(sandbox):
    sb, log = sandbox([("partial\n", MontyCrashedError("worker died")), ("", 1)])
    out = sb.run("boom")
    assert out.startswith("partial") and "reset" in out
    sb.run("1")
    assert log["sessions"] == 2


def test_reset_flag_and_output_cap(sandbox, monkeypatch):
    monkeypatch.setattr(ps, "MAX_OUTPUT_CHARS", 10)
    sb, log = sandbox([("", 1), ("x" * 50, None)])
    sb.run("a = 1")
    out = sb.run("print('x'*50)", reset=True)
    assert log["sessions"] == 2 and "truncated" in out


# ---------------------------------------------------------------------------
# Discovery / registration
# ---------------------------------------------------------------------------

def test_finds_worker_in_pip_target_bin(monkeypatch, tmp_path):
    name = "monty.exe" if ps.sys.platform == "win32" else "monty"
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / name).write_text("")
    monkeypatch.delenv("MONTY_BIN", raising=False)
    monkeypatch.setattr(ps, "_monty", None)
    monkeypatch.setattr(ps.sys, "path", [str(tmp_path)])
    monkeypatch.setattr(ps.shutil, "which", lambda n: None)
    assert ps.find_monty_binary() == str(tmp_path / "bin" / name)
    monkeypatch.setattr(ps.sys, "path", [])
    assert ps.find_monty_binary() is None


def test_not_registered_without_monty_or_when_disabled(monkeypatch):
    registered = []
    monkeypatch.setattr(ps, "_monty", None)
    assert ps.register_python_sandbox_tool(lambda **kw: registered.append(kw)) is False
    monkeypatch.setattr(ps, "is_available", lambda: True)
    monkeypatch.setattr(ps, "sandbox_settings", lambda: {"enabled": False})
    assert ps.register_python_sandbox_tool(lambda **kw: registered.append(kw)) is False
    monkeypatch.setattr(ps, "sandbox_settings", lambda: {"timeout_s": 7})
    assert ps.register_python_sandbox_tool(lambda **kw: registered.append(kw)) is True
    assert registered[0]["name"] == "run_python" and "7 s" in registered[0]["description"]


# ---------------------------------------------------------------------------
# USB installer: pip --target --upgrade must not wipe earlier scripts
# ---------------------------------------------------------------------------

def test_install_keeps_earlier_packages_scripts(monkeypatch, tmp_path):
    from portable import runtime

    def fake_pip(cmd, **kw):
        target = Path(cmd[cmd.index("--target") + 1])
        bin_dir = target / "bin"
        if bin_dir.exists():                     # what pip --upgrade does
            for f in bin_dir.iterdir():
                f.unlink()
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / cmd[-1].split(">")[0]).write_text("script")
        return runtime.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(runtime.subprocess, "run", fake_pip)
    runtime.install_packages(["pydantic-monty>=1.1.0", "cffi>=1"], tmp_path, "linux")
    bin_dir = runtime.site_packages(tmp_path, "linux") / "bin"
    assert sorted(p.name for p in bin_dir.iterdir()) == ["cffi", "pydantic-monty"]


# ---------------------------------------------------------------------------
# Real sandbox (only where pydantic-monty is installed)
# ---------------------------------------------------------------------------

def test_real_sandbox_is_isolated(tmp_path):
    pytest.importorskip("pydantic_monty")
    if not ps.find_monty_binary():
        pytest.skip("monty worker binary not installed")
    sb = ps.PythonSandbox(timeout_s=2, work_dir=tmp_path)
    try:
        assert sb.run("import json\nx = json.loads('[1, 2]')\nsum(x)") == "→ 3"
        assert sb.run("x") == "→ [1, 2]"
        assert "PermissionError" in sb.run("open('/etc/hostname').read()")
        assert "ModuleNotFoundError" in sb.run("import subprocess")
        sb.run("open('/work/out.txt', 'w').write('ok')")
        assert (tmp_path / "out.txt").read_text() == "ok"
        assert "TimeoutError" in sb.run("while True:\n    pass")
    finally:
        sb.close()
