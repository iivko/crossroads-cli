import json
from pathlib import Path

from typer.testing import CliRunner

from market_cli.client import Client, CLIError, save_profile
from market_cli.main import app

runner = CliRunner()


def test_command_tree_has_no_human_approve_command():
    result = runner.invoke(app, ["approvals", "--help"])
    assert result.exit_code == 0
    assert "status" in result.output and "open" in result.output
    result = runner.invoke(app, ["approvals", "approve", "room"])
    assert result.exit_code == 2


def test_json_flags_work_after_nested_commands(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKET_CONFIG", str(tmp_path / "config.json"))
    calls = []

    def mock(self, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"items": []}

    monkeypatch.setattr(Client, "call", mock)
    result = runner.invoke(app, ["products", "list", "--json", "--material", "wood"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"items": []}
    assert calls[0][2]["params"]["material"] == "wood"


def test_permission_errors_are_machine_readable(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKET_CONFIG", str(tmp_path / "config.json"))

    def mock(*args, **kwargs):
        raise CLIError("permission_denied", "Only sellers can edit products", 4)

    monkeypatch.setattr(Client, "call", mock)
    result = runner.invoke(app, ["products", "stock", "set", "prod-forma", "0", "--json"])
    assert result.exit_code == 4
    assert not result.stdout
    assert json.loads(result.stderr)["error"]["code"] == "permission_denied"


def test_credentials_saved_with_private_permissions(monkeypatch, tmp_path):
    path = tmp_path / "auth" / "config.json"
    monkeypatch.setenv("MARKET_CONFIG", str(path))
    save_profile("buyer", {"token": "test-secret"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(path.read_text())["profiles"]["buyer"]["token"] == "test-secret"


def test_media_upload_cannot_escape_allowed_directory(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"not-a-real-image")
    monkeypatch.setenv("MARKET_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MARKET_MEDIA_ROOT", str(allowed))
    result = runner.invoke(app, ["products", "images", "upload", "prod-forma", str(secret), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "media_path_forbidden"


def test_download_outputs_real_file_with_checksum(monkeypatch, tmp_path):
    import hashlib

    content = b"fake-image-fixture"
    checksum = hashlib.sha256(content).hexdigest()
    monkeypatch.setenv("MARKET_CONFIG", str(tmp_path / "config.json"))

    def mock(self, method, path, **kwargs):
        if path.endswith("/images"):
            return [
                {
                    "id": "image-1",
                    "status": "ready",
                    "sha256": checksum,
                    "mime": "image/jpeg",
                    "caption": "Front",
                }
            ]
        return content

    monkeypatch.setattr(Client, "call", mock)
    result = runner.invoke(
        app,
        [
            "products",
            "images",
            "read",
            "prod-forma",
            "--all",
            "--output-dir",
            str(tmp_path / "photos"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    manifest = json.loads(result.stdout)
    assert Path(manifest["images"][0]["path"]).read_bytes() == content
    assert manifest["images"][0]["sha256"] == checksum


def test_remote_plain_http_rejected():
    import pytest

    with pytest.raises(CLIError, match="Remote API connections require HTTPS"):
        Client("http://example.com")


def test_agent_watch_reports_changed_inbox_and_human_request_with_bounded_polling(monkeypatch, tmp_path):
    import copy

    monkeypatch.setenv("MARKET_CONFIG", str(tmp_path / "config.json"))
    now = [0.0]
    calls = []
    initial = {"items": [{"id": "room", "version": 1, "can_act": False, "pending_guidance_id": None}]}
    requested = copy.deepcopy(initial)
    requested["items"][0].update(version=2, can_act=True, pending_guidance_id="own-guidance")
    replies = iter([initial, initial, requested])

    def api(self, method, path, **kwargs):
        calls.append((method, path))
        return next(replies)

    monkeypatch.setattr(Client, "call", api)
    monkeypatch.setattr("market_cli.main.time.monotonic", lambda: now[0])
    monkeypatch.setattr("market_cli.main.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    result = runner.invoke(app, ["agent", "watch", "--timeout", "5", "--interval", "2", "--jsonl"])
    assert result.exit_code == 0, result.output
    assert [json.loads(line) for line in result.stdout.splitlines()] == [initial, requested]
    assert calls == [("GET", "/agent/inbox")] * 3
    assert now[0] == 5


def test_agent_watch_validates_polling_limits():
    for args in (["--timeout", "0"], ["--timeout", "3601"], ["--interval", "0.1"]):
        assert runner.invoke(app, ["agent", "watch", *args]).exit_code == 2
