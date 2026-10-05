"""The leak guard must inspect staged content without exposing private files."""

import json
import socket
import sys
from pathlib import Path

import pytest

from scripts import check_secrets, dev


def test_known_tokens_detected_without_returning_values():
    token = b"sk-" + b"x" * 32
    findings = check_secrets.content_findings(token)
    assert findings == ["provider token"]
    assert token.decode() not in str(findings)
    assert check_secrets.content_findings(b"OPENTALE_API_KEY=\n") == []
    assert check_secrets.content_findings(b"OPENTALE_API_KEY=\nOPENTALE_MODEL=deepseek-flash\n") == []
    assert check_secrets.content_findings(b"OPENTALE_API_KEY=replace-me\n") == []
    assert "non-placeholder API key assignment" in check_secrets.content_findings(
        b"OPENTALE_API_KEY=" + b"secret" * 4
    )


def test_private_path_never_opened(tmp_path: Path, monkeypatch):
    def mocked_git(root, *args):
        assert args[0] == "ls-files"
        return b".env.local\0runtime/run.json\0"

    monkeypatch.setattr(check_secrets, "git", mocked_git)
    findings = check_secrets.scan(tmp_path)
    assert len(findings) == 2
    assert all(reason == "private file present in Git/public source selection" for _, reason in findings)


def test_staged_secret_caught_when_working_tree_is_clean(tmp_path: Path, monkeypatch):
    (tmp_path / "sample.py").write_text("safe public source", encoding="utf-8")
    token = b"sk-" + b"x" * 32

    def mocked_git(root, *args):
        return token if args[0] == "show" else b"sample.py\0"

    monkeypatch.setattr(check_secrets, "git", mocked_git)
    findings = check_secrets.scan(tmp_path)
    assert findings == [("sample.py", "Git index: provider token")]


def test_stop_never_kills_reused_pid(tmp_path: Path, monkeypatch):
    state = tmp_path / "server-process.json"
    state.write_text(json.dumps({"pid": 9999, "port": 8231, "token": "stale"}), encoding="utf-8")
    monkeypatch.setattr(dev, "STATE", state)
    monkeypatch.setattr(dev, "identity_matches", lambda *_: False)

    def fail_if_called(*_):
        raise AssertionError("must not terminate a reused PID")

    monkeypatch.setattr(dev.os, "kill", fail_if_called)
    dev.stop()
    assert not state.exists()


def test_custom_port_updates_origin_configuration(monkeypatch):
    monkeypatch.setenv("OPENTALE_PORT", "8231")
    assert dev.server_environment(8232)["OPENTALE_PORT"] == "8232"
    assert dev.os.environ["OPENTALE_PORT"] == "8231"


def test_port_permission_failure_not_misreported_as_occupation(monkeypatch):
    class DeniedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def bind(self, *_):
            raise PermissionError

    monkeypatch.setattr(socket, "socket", DeniedSocket)
    with pytest.raises(SystemExit, match="监听权限"):
        dev.assert_port_free(8231)


@pytest.mark.parametrize("action", ["status", "stop", "detach"])
def test_process_identity_permission_failure_safe_cli_preserves_record(tmp_path, monkeypatch, capsys, action):
    state = tmp_path / "server-process.json"
    state.write_text(json.dumps({"pid": 9999, "port": 8231, "token": "private-marker"}), encoding="utf-8")
    monkeypatch.setattr(dev, "STATE", state)
    monkeypatch.setattr(sys, "argv", ["dev.py", action])

    def denied(*_, **__):
        raise PermissionError("details-must-not-be-public")

    def never_kill(*_):
        raise AssertionError("identity unavailable: no signal may be sent")

    monkeypatch.setattr(dev.subprocess, "run", denied)
    monkeypatch.setattr(dev.os, "kill", never_kill)
    with pytest.raises(SystemExit, match="权限限制") as caught:
        dev.main()
    assert caught.value.__suppress_context__
    assert "details-must-not-be-public" not in str(caught.value)
    assert "private-marker" not in str(caught.value)
    assert "没有本脚本管理的服务" not in capsys.readouterr().out
    assert state.exists()
