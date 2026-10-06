"""Process, database and frontend separation without starting any model or server."""

import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import dev


def isolated_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "STATE", tmp_path / "runtime" / "server-process.json")


def test_profile_environment_isolated_and_default_environment_preserved(monkeypatch, tmp_path):
    isolated_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENTALE_PORT", "8231")
    monkeypatch.setenv("OPENTALE_DATA_DIR", str(tmp_path / "main-data"))
    monkeypatch.setenv("OPENTALE_FRONTEND_DIR", str(tmp_path / "main-web"))
    monkeypatch.setenv("OPENTALE_ROOT", str(tmp_path / "config-root"))

    def no_private_file_read(*_, **__):
        raise AssertionError("process configuration must not open any private file")

    monkeypatch.setattr(Path, "read_text", no_private_file_read)
    classic = dev.server_environment(8231)
    reflexive = dev.server_environment(8232, "reflexive")
    assert classic["OPENTALE_DATA_DIR"] == str(tmp_path / "main-data")
    assert classic["OPENTALE_FRONTEND_DIR"] == str(tmp_path / "main-web")
    assert reflexive["OPENTALE_DATA_DIR"] == str(tmp_path / "runtime" / "reflexive" / "data")
    assert reflexive["OPENTALE_FRONTEND_DIR"] == str(tmp_path / "runtime" / "reflexive" / "frontend-dist")
    assert reflexive["OPENTALE_ROOT"] == classic["OPENTALE_ROOT"] == str(tmp_path / "config-root")
    assert reflexive["OPENTALE_PORT"] == "8232"
    assert dev.os.environ["OPENTALE_DATA_DIR"] == str(tmp_path / "main-data")


def test_profile_command_forwarded_to_detached_child_only():
    default = dev.server_command(8231, "fixture-token")
    reflexive = dev.server_command(8232, "fixture-token", "reflexive")
    assert "--profile" not in default
    assert reflexive[-2:] == ["--profile", "reflexive"]
    assert reflexive[reflexive.index("--port") + 1] == "8232"
    assert "_child" in reflexive and "fixture-token" in reflexive
    assert "--profile" not in dev.server_command(8232, profile="reflexive")


@pytest.mark.parametrize("requested, actual, matches", [
    (None, None, True), ("reflexive", "reflexive", True),
    (None, "reflexive", False), ("reflexive", None, False),
])
def test_pid_identity_requires_the_exact_profile(monkeypatch, requested, actual, matches):
    command = dev.server_command(8232, "fixture-token", actual)
    result = SimpleNamespace(returncode=0, stderr="", stdout=shlex.join(command))
    monkeypatch.setattr(dev.subprocess, "run", lambda *_, **__: result)
    assert dev.identity_matches(123, "fixture-token", requested) is matches
    assert dev.identity_matches(123, "fixture-token-other", requested) is False


def test_profile_build_only_writes_its_own_bundle(monkeypatch, tmp_path):
    isolated_paths(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(dev.shutil, "which", lambda _: "/fixture/npm")
    monkeypatch.setattr(dev.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    dev.build("reflexive")
    command, options = calls[0]
    assert command == ["/fixture/npm", "run", "build", "--", "--outDir",
                       str(tmp_path / "runtime" / "reflexive" / "frontend-dist"), "--emptyOutDir"]
    assert options["cwd"] == tmp_path / "frontend" and options["check"] is True
    dev.build()
    assert calls[1][0] == ["/fixture/npm", "run", "build"]


def test_detach_profile_keeps_main_process_and_bundle_untouched(monkeypatch, tmp_path):
    isolated_paths(monkeypatch, tmp_path)
    dev.STATE.parent.mkdir(parents=True)
    main = {"pid": 100, "port": 8231, "token": "main-fixture-token"}
    dev.STATE.write_text(json.dumps(main), encoding="utf-8")
    main_bundle = dev.frontend_directory() / "index.html"
    main_bundle.parent.mkdir(parents=True)
    main_bundle.write_text("classic bundle", encoding="utf-8")
    own_bundle = dev.frontend_directory("reflexive") / "index.html"
    own_bundle.parent.mkdir(parents=True)
    own_bundle.write_text("reflexive bundle", encoding="utf-8")
    calls = []
    monkeypatch.setattr(dev, "assert_port_free", lambda port: calls.append(("port", port)))
    monkeypatch.setattr(dev, "health", lambda _: True)
    monkeypatch.setattr(dev.subprocess, "Popen", lambda command, **kwargs: (
        calls.append((command, kwargs)) or SimpleNamespace(pid=200, poll=lambda: None)
    ))
    dev.detach(8232, "reflexive")
    record = dev.read_state("reflexive")
    assert record["pid"] == 200 and record["port"] == 8232 and record["profile"] == "reflexive"
    assert dev.read_state() == main and main_bundle.read_text(encoding="utf-8") == "classic bundle"
    assert (dev.state_path("reflexive").parent / "server.log").exists()
    assert not (dev.STATE.parent / "server.log").exists()
    command, options = calls[1]
    assert command[-2:] == ["--profile", "reflexive"] and record["token"] in command
    assert options["env"]["OPENTALE_DATA_DIR"] == str(tmp_path / "runtime" / "reflexive" / "data")
    assert options["env"]["OPENTALE_FRONTEND_DIR"] == str(own_bundle.parent)
    assert options["start_new_session"] is True


def test_stop_profile_only_signals_its_own_record(monkeypatch, tmp_path):
    isolated_paths(monkeypatch, tmp_path)
    dev.STATE.parent.mkdir(parents=True)
    main = {"pid": 100, "port": 8231, "token": "main-fixture-token"}
    dev.STATE.write_text(json.dumps(main), encoding="utf-8")
    own = dev.state_path("reflexive")
    own.parent.mkdir(parents=True)
    own.write_text(json.dumps({"pid": 200, "port": 8232, "token": "own-fixture-token",
                               "profile": "reflexive"}), encoding="utf-8")
    calls = []

    def matches(pid, token, profile):
        calls.append((pid, token, profile))
        return len(calls) == 1

    monkeypatch.setattr(dev, "identity_matches", matches)
    signals = []
    monkeypatch.setattr(dev.os, "kill", lambda *args: signals.append(args))
    dev.stop("reflexive")
    assert signals == [(200, dev.signal.SIGTERM)]
    assert all(call == (200, "own-fixture-token", "reflexive") for call in calls)
    assert not own.exists() and dev.read_state() == main


def test_misplaced_profile_record_refused_and_preserved(monkeypatch, tmp_path):
    isolated_paths(monkeypatch, tmp_path)
    own = dev.state_path("reflexive")
    own.parent.mkdir(parents=True)
    own.write_text(json.dumps({"pid": 100, "port": 8231, "token": "main-fixture-token"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="运行配置不符"):
        dev.stop("reflexive")
    assert own.exists()


def test_child_applies_profile_to_api_import_environment(monkeypatch, tmp_path):
    isolated_paths(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(sys, "argv", ["dev.py", "_child", "--profile", "reflexive", "--port", "8232"])
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *args, **kwargs: calls.append((
        args, kwargs, {name: dev.os.environ[name] for name in (
            "OPENTALE_PORT", "OPENTALE_DATA_DIR", "OPENTALE_FRONTEND_DIR",
        )},
    ))))
    for name in ("OPENTALE_PORT", "OPENTALE_DATA_DIR", "OPENTALE_FRONTEND_DIR"):
        monkeypatch.setenv(name, "fixture-before")
    dev._main()
    args, options, environment = calls[0]
    assert args == ("opentale.api:app",) and options["port"] == 8232
    assert environment == {"OPENTALE_PORT": "8232",
                           "OPENTALE_DATA_DIR": str(tmp_path / "runtime" / "reflexive" / "data"),
                           "OPENTALE_FRONTEND_DIR": str(tmp_path / "runtime" / "reflexive" / "frontend-dist")}
