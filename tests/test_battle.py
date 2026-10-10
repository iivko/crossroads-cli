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


def test_external_follow_waits_for_new_run_and_redacts_human_instruction(config):
    snapshots = [room("awaiting_approval"), room(), room("ordered")]
    snapshots[0]["run"] = 1
    snapshots[1]["run"] = snapshots[2]["run"] = 2
    snapshots[1]["activity"].append(
        {
            "id": "guidance-event",
            "type": "human_requested_negotiation",
            "role": "buyer",
            "data": {"instruction": "private-tactic-do-not-display", "run": 2},
        }
    )
    snapshots[1]["human_guidance"] = [{"instruction": "private-tactic-do-not-display"}]
    now = [0.0]

    class Adapter:
        def __init__(self, *args):
            pass

        def verify_identity(self):
            pass

        def room(self):
            return snapshots.pop(0)

    journal = Journal(config["run_dir"], config["room_id"])
    result = run_external(
        config,
        journal,
        wait_for_guidance=True,
        adapter=Adapter,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    assert result["status"] == "finished" and result["api_run"] == 2
    assert result["api_state"] == "ordered" and now[0] == 2
    for side in ("buyer", "seller"):
        text = (journal.directory / f"{side}.jsonl").read_text()
        assert "waiting_for_guidance" in text and "human_requested_negotiation" in text
        assert "private-tactic" not in text and "instruction" not in text


@pytest.mark.parametrize("state", ["awaiting_approval", "declined", "needs_human_review", "expired"])
def test_external_follow_remains_bounded_while_waiting(config, state):
    now = [0.0]

    class Adapter:
        def __init__(self, *args):
            pass

        def verify_identity(self):
            pass

        def room(self):
            return room(state)

    journal = Journal(config["run_dir"], config["room_id"])
    result = run_external(
        config,
        journal,
        wait_for_guidance=True,
        adapter=Adapter,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    assert result["status"] == "timeout" and now[0] == config["timeout_seconds"]
    events = [
        json.loads(line)["event"] for line in (journal.directory / "buyer.jsonl").read_text().splitlines()
    ]
    assert events.count("waiting_for_guidance") == 1


@pytest.mark.parametrize("state", ["ordered", "cancelled"])
def test_external_follow_stops_when_further_guidance_is_impossible(config, state):
    class Adapter:
        def __init__(self, *args):
            pass

        def verify_identity(self):
            pass

        def room(self):
            return room(state)

    journal = Journal(config["run_dir"], config["room_id"])
    result = run_external(config, journal, wait_for_guidance=True, adapter=Adapter)
    assert result["status"] == "finished" and result["api_state"] == state


def test_new_run_events_follow_chronology_and_preserve_buyer_proposal_label(config):
    import copy

    first = room("awaiting_approval")
    first["offers"][0]["created_at"] = "2026-10-10T10:00:00+00:00"
    first["activity"][0].update(at="2026-10-10T10:00:01+00:00", data={"offer_id": "offer-1"})
    journal = Journal(config["run_dir"], config["room_id"])
    journal.observe(first)

    reopened = copy.deepcopy(first)
    reopened.update(state="negotiating", run=2, version=4)
    proposal = copy.deepcopy(first["offers"][0])
    proposal.update(id="buyer-revised-offer", sender="buyer", created_at="2026-10-10T10:02:00+00:00")
    reopened["offers"].append(proposal)
    # Deliberately shuffled: chronology comes from actual event timestamps,
    # including timezone offsets, not the order of either source collection.
    reopened["activity"] = [
        {
            "id": "buyer-propose",
            "type": "propose",
            "role": "buyer",
            "data": {"offer_id": proposal["id"], "private": "private-tactic-never-print"},
            "at": "2026-10-10T12:02:01+02:00",
        },
        *first["activity"],
        {
            "id": "new-attempt",
            "type": "human_requested_negotiation",
            "role": "buyer",
            "data": {"run": 2, "instruction": "private-tactic-never-print"},
            "at": "2026-10-10T10:01:00Z",
        },
    ]
    reopened["human_guidance"] = [{"instruction": "private-tactic-never-print"}]
    restored = Journal(config["run_dir"], config["room_id"], resume=True)
    restored.observe(reopened)
    restored.observe(reopened)
    for side in ("buyer", "seller"):
        text = (journal.directory / f"{side}.jsonl").read_text()
        events = [json.loads(line) for line in text.splitlines()]
        assert [event["event"] for event in events] == ["propose", "human_requested_negotiation", "propose"]
        assert events[-1]["role"] == "buyer"
        assert "seller" not in events[-1]["message"].lower()
        assert "private-tactic" not in text and "instruction" not in text
    assert restored.state["seen_offers"] == ["offer-1", "buyer-revised-offer"]
    assert set(restored.state["seen_activity"]) == {"event-1", "new-attempt", "buyer-propose"}


def test_observer_uses_offer_creation_time_when_action_timestamp_missing(config):
    value = room()
    value["offers"][0]["created_at"] = "2026-10-10T10:02:00+00:00"
    value["activity"] = [
        {
            "id": "new-attempt",
            "type": "human_requested_negotiation",
            "role": "seller",
            "data": {},
            "at": "2026-10-10T10:01:00+00:00",
        },
        {"id": "offer-action", "type": "counter", "role": "seller", "data": {"offer_id": "offer-1"}},
    ]
    journal = Journal(config["run_dir"], config["room_id"])
    journal.observe(value)
    events = [
        json.loads(line)["event"] for line in (journal.directory / "buyer.jsonl").read_text().splitlines()
    ]
    assert events == ["human_requested_negotiation", "counter"]
