import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from market_cli.battle.io import BattleError, Marketplace, load_config, private_json
from market_cli.battle.runtime import Journal, public_terms, run_external, run_lock
from market_cli.main import app


@pytest.fixture
def config(tmp_path):
    return {
        "api_url": "http://127.0.0.1:8100",
        "room_id": "room-1",
        "run_dir": str(tmp_path / "run"),
        "buyer": {"workspace_id": "buyer-ws", "token": "private-buyer-token"},
        "seller": {"workspace_id": "seller-ws", "token": "private-seller-token"},
        "timeout_seconds": 10,
        "max_model_calls": 8,
        "pause_seconds": 0,
    }


def room(state="negotiating"):
    return {
        "state": state,
        "version": 1,
        "next_role": "buyer",
        "private_rules": {"min_unit_price": 10000},
        "request": {"max_total": 120000, "secret": "buyer-private-budget"},
        "offers": [
            {
                "id": "offer-1",
                "sender": "seller",
                "terms": {
                    "unit_price": 14000,
                    "quantity": 8,
                    "total": 136000,
                    "deposit_percent": 30,
                    "delivery_charge": 24000,
                    "delivery_date": "2026-11-01",
                    "unexpected_secret": "private-seller-token",
                },
            }
        ],
        "activity": [{"id": "event-1", "type": "propose", "role": "seller", "data": {"token": "secret"}}],
    }


def test_private_config_permissions_and_role_separation(config, tmp_path):
    path = tmp_path / "config.json"
    private_json(path, config)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_config(path)["buyer"]["workspace_id"] == "buyer-ws"
    path.chmod(0o644)
    with pytest.raises(BattleError, match="private_file_must_be_mode_0600"):
        load_config(path)
    config["seller"]["token"] = config["buyer"]["token"]
    private_json(path, config)
    with pytest.raises(BattleError, match="distinct_credentials"):
        load_config(path)


def test_adapter_supplies_only_its_role_credentials_without_shell(config, tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_TOKEN", "inherited-admin-secret")
    monkeypatch.setenv("MARKET_WORKSPACE", "admin-ws")
    monkeypatch.setenv("MARKET_PROFILE", "admin")
    captured = []

    def run(args, **kwargs):
        captured.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"role": "buyer"}), stderr="")

    monkeypatch.setattr("market_cli.battle.io.subprocess.run", run)
    client = Marketplace(config, "buyer", tmp_path)
    assert client.context()["role"] == "buyer"
    args, opts = captured[0]
    assert args[1:] == ["-m", "market_cli.main", "negotiations", "context", "room-1", "--json"]
    assert "shell" not in opts
    assert opts["env"]["MARKET_TOKEN"] == "private-buyer-token"
    assert "private-seller-token" not in json.dumps(opts)
    assert "inherited-admin-secret" not in json.dumps(opts)
    assert "MARKET_PROFILE" not in opts["env"]
    assert config["buyer"]["token"] not in " ".join(args)


def test_adapter_requires_agent_identity(config, tmp_path, monkeypatch):
    client = Marketplace(config, "buyer", tmp_path)
    identity = {
        "workspace": {"id": "buyer-ws", "role": "buyer"},
        "credential": {
            "kind": "cli",
            "scopes": ["catalogue:read", "images:read", "negotiations:read", "negotiations:act"],
        },
    }
    monkeypatch.setattr(client, "_run", lambda _: identity)
    with pytest.raises(BattleError, match="restricted_agent"):
        client.verify_identity()
    identity["credential"]["kind"] = "agent"
    client.verify_identity()
    identity["workspace"]["role"] = "seller"
    with pytest.raises(BattleError, match="restricted_agent"):
        client.verify_identity()


def test_public_events_drop_all_private_fields_and_untrusted_strings(config):
    journal = Journal(config["run_dir"], config["room_id"])
    journal.observe(room())
    journal.observe(room())
    text = (journal.directory / "buyer.jsonl").read_text()
    assert len(text.splitlines()) == 1
    assert "14000" in text and "seller" in text
    for forbidden in ("private", "secret", "max_total", "min_unit_price", "unexpected_secret"):
        assert forbidden not in text
    assert public_terms({"unit_price": "\x1b[31mprivate-secret", "delivery_date": "not-a-date"}) == {}


def test_external_monitor_finishes_on_agreement_without_creating_order(config):
    calls = []

    class Adapter:
        def __init__(self, config, role, run_dir):
            self.role = role

        def verify_identity(self):
            calls.append((self.role, "identity"))

        def room(self):
            calls.append((self.role, "room"))
            return room("awaiting_approval")

    journal = Journal(config["run_dir"], config["room_id"])
    result = run_external(config, journal, adapter=Adapter)
    assert result["status"] == "finished" and result["api_state"] == "awaiting_approval"
    assert calls == [("buyer", "identity"), ("seller", "identity"), ("buyer", "room")]
    assert "private_rules" not in result


def test_external_timeout_is_bounded_and_explicit(config):
    clock = iter([0, 0, 11, 11])

    class Adapter:
        def __init__(self, *args):
            pass

        def verify_identity(self):
            pass

        def room(self):
            return room()

    journal = Journal(config["run_dir"], config["room_id"])
    result = run_external(config, journal, adapter=Adapter, clock=lambda: next(clock), sleep=lambda _: None)
    assert result["status"] == "timeout"
    assert "Participants remain independent" in (journal.directory / "buyer.jsonl").read_text()


def test_state_resume_preserves_seen_offers_and_rejects_other_room(config):
    journal = Journal(config["run_dir"], config["room_id"])
    journal.observe(room())
    with pytest.raises(BattleError, match="use_resume"):
        Journal(config["run_dir"], config["room_id"])
    restored = Journal(config["run_dir"], config["room_id"], resume=True)
    restored.observe(room())
    assert len((restored.directory / "buyer.jsonl").read_text().splitlines()) == 1
    with pytest.raises(BattleError, match="resume_room_mismatch"):
        Journal(config["run_dir"], "other-room", resume=True)


def test_only_one_controller_can_hold_lock(config):
    with run_lock(config["run_dir"]):
        with pytest.raises(BattleError, match="already_running"):
            with run_lock(config["run_dir"]):
                pass


def test_terminal_watch_and_status_show_no_private_state(config, monkeypatch, tmp_path):
    monkeypatch.setenv("MARKET_CONFIG", str(tmp_path / "missing.json"))
    journal = Journal(config["run_dir"], config["room_id"])
    journal.state["pending"] = {"claim_token": "never-show-this"}
    journal.observe(room("awaiting_approval"))
    journal.emit("finished", room_state="awaiting_approval")
    runner = CliRunner()
    result = runner.invoke(app, ["battle", "watch", "--run-dir", config["run_dir"], "--role", "seller"])
    assert result.exit_code == 0, result.output
    assert "€140.00" in result.output and "SELLER" in result.output
    assert "never-show-this" not in result.output
    result = runner.invoke(app, ["battle", "status", "--run-dir", config["run_dir"]])
    assert result.exit_code == 0 and "pending" not in result.output


def test_adapter_checks_actual_image_content(config, tmp_path, monkeypatch):
    import base64
    import hashlib

    client = Marketplace(config, "buyer", tmp_path)
    image_dir = client.directory / "images"
    image_dir.mkdir()
    path = image_dir / "photo.jpg"
    content = b"actual-downloaded-photo-fixture"
    path.write_bytes(content)
    value = {
        "images": [{"path": str(path), "mime": "image/jpeg", "sha256": hashlib.sha256(content).hexdigest()}]
    }
    monkeypatch.setattr(client, "_run", lambda _: value)
    blocks = client.images("product")
    assert base64.b64decode(blocks[0]["base64"]) == content
    path.write_bytes(b"tampered")
    with pytest.raises(BattleError, match="checksum"):
        client.images("product")
