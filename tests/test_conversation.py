import json

import pytest
from typer.testing import CliRunner

from market_cli.battle.runtime import Journal
from market_cli.client import Client
from market_cli.conversation import public_messages, terminal_text
from market_cli.main import app


def message(
    identifier="m1", text="Can we lower the upfront payment?", role="buyer", at="2026-10-10T10:00:00Z"
):
    return {
        "id": identifier,
        "role": role,
        "type": "message",
        "run": 2,
        "data": {"message": text, "purpose": "question", "image_ids": ["gallery-image"]},
        "at": at,
    }


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKET_CONFIG", str(tmp_path / "config.json"))
    return CliRunner()


def test_say_preserves_explicit_claim_guidance_and_retry_payload(cli, monkeypatch, tmp_path):
    original = {
        "message": "Can you offer a smaller deposit?",
        "purpose": "question",
        "expected_version": 3,
        "claim_token": "private-claim",
        "guidance_id": "own-guidance",
        "image_ids": ["actual-image"],
    }
    path = tmp_path / "message.json"
    path.write_text(json.dumps(original))
    captured = []

    def call(self, method, endpoint, **kwargs):
        captured.append((method, endpoint, kwargs["data"], self.key))
        return {"state": "negotiating", "action_id": "action-one"}

    monkeypatch.setattr(Client, "call", call)
    args = ["negotiations", "say", "room", "--file", str(path), "--idempotency-key", "one-message-key"]
    assert cli.invoke(app, args).exit_code == 0
    assert cli.invoke(app, args).exit_code == 0
    assert (
        captured
        == [("POST", "/negotiations/room/actions", {**original, "type": "message"}, "one-message-key")] * 2
    )
    assert json.loads(path.read_text()) == original
    original["type"] = "accept"
    path.write_text(json.dumps(original))
    rejected = cli.invoke(app, args)
    assert rejected.exit_code == 2 and len(captured) == 2


def test_brief_set_requires_explicit_document_and_preserves_owner_versions(cli, monkeypatch, tmp_path):
    document = {
        "expected_version": 7,
        "expected_brief_version": 2,
        "goals": "private owner preference",
        "shareable_needs": "Reduce upfront payment",
        "requirements": [],
    }
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(document))
    seen = []

    def call(self, method, endpoint, **kwargs):
        seen.append((method, endpoint, kwargs.get("data"), self.key))
        return {"version": 3, "goals": document["goals"], "requirements": []}

    monkeypatch.setattr(Client, "call", call)
    result = cli.invoke(
        app, ["negotiations", "brief", "set", "room", "--file", str(path), "--idempotency-key", "brief-key"]
    )
    assert result.exit_code == 0, result.output
    assert seen == [("PUT", "/negotiations/room/brief", document, "brief-key")]
    assert cli.invoke(app, ["negotiations", "brief", "get", "room"]).exit_code == 0
    assert seen[1][:2] == ("GET", "/negotiations/room/brief")


def test_message_command_pagination_and_run_filter_preserves_api_page(cli, monkeypatch):
    page = {"items": [message()], "total": 8, "limit": 2, "offset": 3}

    def call(self, method, endpoint, **kwargs):
        assert (method, endpoint) == ("GET", "/negotiations/room/messages")
        assert kwargs["params"] == {"limit": 2, "offset": 3, "run": 2}
        return page

    monkeypatch.setattr(Client, "call", call)
    result = cli.invoke(
        app, ["negotiations", "messages", "room", "--limit", "2", "--offset", "3", "--run", "2"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == page


def test_message_watch_reads_all_pages_then_emits_only_new_public_messages(cli, monkeypatch):
    now = [0.0]
    initial = [message("m1"), message("m2")]
    later = [*initial, message("m3")]
    calls = []
    monkeypatch.setattr("market_cli.main.time.monotonic", lambda: now[0])
    monkeypatch.setattr("market_cli.main.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    def call(self, method, endpoint, **kwargs):
        assert endpoint.endswith("/messages")
        assert method == "GET"
        values = initial if now[0] == 0 else later
        offset, limit = kwargs["params"]["offset"], kwargs["params"]["limit"]
        calls.append((now[0], offset))
        return {
            "items": values[offset : offset + limit],
            "total": len(values),
            "offset": offset,
            "limit": limit,
        }

    monkeypatch.setattr(Client, "call", call)
    result = cli.invoke(
        app,
        [
            "negotiations",
            "messages",
            "room",
            "--watch",
            "--timeout",
            "3",
            "--interval",
            "2",
            "--limit",
            "1",
            "--jsonl",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [json.loads(line)["id"] for line in result.stdout.splitlines()] == ["m1", "m2", "m3"]
    assert calls == [(0, 0), (0, 1), (2, 0), (2, 1), (2, 2)]
    assert now[0] == 3


def test_public_projection_never_includes_private_review_question_or_context():
    public = message()
    public["data"].update(
        human_question="owner-only-question", goals="private-goals", credentials="secret-token"
    )
    room = {
        "activity": [
            public,
            {
                "id": "private-question",
                "type": "request_human_review",
                "role": "seller",
                "data": {"message": "owner-only-question"},
            },
        ],
        "private_brief": {"goals": "private-goals"},
        "human_questions": [{"question": "owner-only-question"}],
    }
    text = json.dumps(public_messages(room))
    assert "upfront payment" in text
    assert all(
        secret not in text
        for secret in ("owner-only-question", "private-goals", "secret-token", "human_question")
    )


def test_text_view_neutralizes_terminal_controls_and_never_executes_markup(cli, monkeypatch):
    text = "Price €115\x1b[2J\rHIDE\u202e plus delivery\n[link=https://example.test]plain[/link]"
    monkeypatch.setattr(Client, "call", lambda *args, **kwargs: {"items": [message(text=text)], "total": 1})
    result = cli.invoke(app, ["negotiations", "messages", "room", "--text"])
    assert result.exit_code == 0
    assert "Price €115" in result.stdout and "[link=" in result.stdout
    assert "\x1b" not in result.stdout and "\r" not in result.stdout and "\u202e" not in result.stdout
    assert len(terminal_text("a" * 2500)) == 2000


def test_battle_logs_real_dialogue_and_offer_explanations_but_not_owner_question(tmp_path):
    journal = Journal(tmp_path, "room")
    offer = {
        "id": "o1",
        "sender": "seller",
        "created_at": "2026-10-10T10:02:00Z",
        "terms": {
            "unit_price": 11500,
            "quantity": 8,
            "total": 116000,
            "deposit_percent": 25,
            "delivery_charge": 24000,
            "delivery_date": "2026-10-22",
        },
    }
    revision = {
        "id": "revision",
        "role": "seller",
        "type": "revise",
        "at": "2026-10-10T10:02:01Z",
        "data": {
            "offer_id": "o1",
            "message": "€115 with a 25% deposit protects upfront cash flow.",
            "purpose": "explanation",
        },
    }
    review = {
        "id": "review",
        "role": "buyer",
        "type": "request_human_review",
        "at": "2026-10-10T10:03:00Z",
        "data": {"human_question": "PRIVATE: which financial priority wins?"},
    }
    room = {
        "state": "needs_human_review",
        "version": 4,
        "run": 2,
        "next_role": "buyer",
        "offers": [offer],
        "activity": [message(text="Could we lower the deposit?\x1b[2J"), revision, review],
        "human_questions": [{"question": "PRIVATE: which financial priority wins?"}],
        "private_brief": {"goals": "private-goals"},
    }
    journal.observe(room)
    journal.observe(room)
    journal.emit("finished", room_state=room["state"])
    for side in ("buyer", "seller"):
        text = (tmp_path / f"{side}.jsonl").read_text()
        records = [json.loads(line) for line in text.splitlines()]
        assert [record["event"] for record in records] == [
            "message",
            "revise",
            "request_human_review",
            "finished",
        ]
        assert "Could we lower the deposit?" in text and "protects upfront cash flow" in text
        assert "PRIVATE" not in text and "private-goals" not in text and "\\u001b" not in text
    result = CliRunner().invoke(app, ["battle", "watch", "--run-dir", str(tmp_path), "--role", "buyer"])
    assert result.exit_code == 0, result.output
    assert "BUYER: QUESTION" in result.output and "SELLER: REVISE" in result.output
    assert "Could we lower the deposit?" in result.output and "PRIVATE" not in result.output
