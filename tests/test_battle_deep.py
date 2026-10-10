"""Optional adapter checks; the ordinary CLI test environment needs no LLM SDK."""

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("deepagents")

from market_cli.battle.deep import Decision, Participant, run_deepagents, validate_decision
from market_cli.battle.io import BattleError
from market_cli.battle.runtime import Journal


def context(role="seller", version=1):
    delivery = (datetime.now(UTC).date() + timedelta(days=12)).isoformat()
    return {
        "role": role,
        "version": version,
        "state": "negotiating",
        "run": 1,
        "next_role": role,
        "product_id": "product-1",
        "product": {"unit_price": 14000, "delivery_charge": 24000, "lead_days": 12},
        "request": {"quantity": 8, "latest_delivery": delivery},
        "private_rules": (
            {
                "min_unit_price": 10000,
                "max_discount_percent": 100,
                "min_deposit_percent": 20,
                "discount_min_quantity": 1,
            }
            if role == "seller"
            else {"max_total": 120000, "max_deposit_percent": 30}
        ),
        "allowed_actions": ["propose", "counter", "accept", "decline", "request_human_review"],
        "offers": [],
        "activity": [],
        "current_offer": None,
        "human_guidance": [],
    }


def proposal(price=11000):
    return Decision(
        type="propose",
        terms={
            "unit_price": price,
            "quantity": 8,
            "delivery_date": context()["request"]["latest_delivery"],
            "deposit_percent": 25,
        },
    )


def test_own_policy_bounds_and_counter_limit_are_checked_before_claim():
    validate_decision(proposal(), context())
    with pytest.raises(BattleError, match="own_policy"):
        validate_decision(proposal(9999), context())
    with pytest.raises(BattleError, match="own_policy"):
        validate_decision(proposal(12001), context("buyer"))
    exhausted = context()
    exhausted["allowed_actions"] = ["accept", "decline", "request_human_review"]
    with pytest.raises(BattleError, match="not_allowed"):
        validate_decision(proposal().model_copy(update={"type": "counter"}), exhausted)


def test_agent_constructs_without_shell_or_delegation_tools(tmp_path):
    journal = Journal(tmp_path, "room-1")
    client = SimpleNamespace(directory=tmp_path, context=lambda: {}, product=lambda p: {})
    participant = Participant(
        {"model": "openai:gpt-4.1-mini", "model_kwargs": {"api_key": "unit-test-placeholder"}},
        "buyer",
        client,
        journal,
    )
    tools = participant.agent.nodes["tools"].bound.tools_by_name
    assert "inspect_current_negotiation" in tools and "inspect_product" in tools
    assert "inspect_shared_conversation" in tools and "inspect_private_brief" in tools
    assert not {"execute", "task", "eval"} & tools.keys()


def test_role_memory_and_actual_image_inputs_are_isolated(tmp_path, monkeypatch):
    captures = []

    class FakeAgent:
        def invoke(self, value, options):
            captures.append(value)
            return {"messages": value["messages"], "files": value["files"], "structured_response": proposal()}

    monkeypatch.setattr("market_cli.battle.deep.create_deep_agent", lambda **kwargs: FakeAgent())
    monkeypatch.setattr("market_cli.battle.deep.init_chat_model", lambda *args, **kwargs: object())
    journal = Journal(tmp_path, "room-1")
    for role in ("buyer", "seller"):
        directory = tmp_path / role
        directory.mkdir(mode=0o700)
        client = SimpleNamespace(
            directory=directory,
            context=lambda: {},
            product=lambda _: {},
            images=lambda _: [{"type": "image", "base64": "cmVhbC1waG90bw==", "mime_type": "image/jpeg"}],
        )
        p = Participant({"model": "test:model"}, role, client, journal)
        own_context = context(role)
        own_context["human_guidance"] = [
            {"id": f"{role}-guidance", "status": "pending", "instruction": f"{role}-private-tactic"}
        ]
        own_context["private_brief"] = {"version": 1, "goals": f"{role}-private-goal", "requirements": []}
        own_context["human_questions"] = [{"question": f"{role}-private-question"}]
        p.decide(own_context)
    buyer_text = captures[0]["messages"][-1].content[0]["text"]
    seller_text = captures[1]["messages"][-1].content[0]["text"]
    assert "max_total" in buyer_text and "min_unit_price" not in buyer_text
    assert "min_unit_price" in seller_text and "max_total" not in seller_text
    assert "buyer-private-tactic" in buyer_text and "seller-private-tactic" not in buyer_text
    assert "seller-private-tactic" in seller_text and "buyer-private-tactic" not in seller_text
    assert "buyer-private-goal" in buyer_text and "seller-private-goal" not in buyer_text
    assert "seller-private-question" in seller_text and "buyer-private-question" not in seller_text
    assert captures[0]["messages"][-1].content[1]["base64"] == "cmVhbC1waG90bw=="
    assert len(captures[1]["messages"]) == 1
    assert (tmp_path / "buyer" / "memory.json").stat().st_mode & 0o777 == 0o600


def test_real_deep_agent_graph_structured_output_vision_and_model_budget(tmp_path):
    import httpx

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "unit-test-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4.1-mini",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "decision-1",
                                    "type": "function",
                                    "function": {
                                        "name": "Decision",
                                        "arguments": proposal().model_dump_json(),
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    journal = Journal(tmp_path, "room-1")
    journal.state["max_model_calls"] = 1
    client = SimpleNamespace(
        directory=tmp_path,
        context=lambda: context(),
        product=lambda _: {},
        images=lambda _: [{"type": "image", "base64": "cmVhbC1waG90bw==", "mime_type": "image/jpeg"}],
    )
    p = Participant(
        {
            "model": "openai:gpt-4.1-mini",
            "model_kwargs": {
                "api_key": "unit-test-placeholder",
                "http_client": httpx.Client(transport=httpx.MockTransport(respond)),
            },
        },
        "seller",
        client,
        journal,
    )
    assert p.decide(context()).terms.unit_price == 11000
    assert journal.state["model_calls"] == 1
    encoded = json.dumps(requests[0])
    assert "data:image/jpeg;base64,cmVhbC1waG90bw==" in encoded
    names = {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert "Decision" in names and not {"execute", "task", "eval"} & names
    with pytest.raises(BattleError, match="model_call_limit_reached"):
        p.decide(context())
    assert len(requests) == 1


def test_ambiguous_submission_replays_identical_payload_and_key_before_new_decision(tmp_path):
    config = {
        "room_id": "room-1",
        "run_dir": str(tmp_path),
        "buyer": {},
        "seller": {},
        "max_model_calls": 12,
        "timeout_seconds": 30,
        "pause_seconds": 0,
    }
    state = {"room": context(), "ambiguous": True, "actions": [], "decisions": [], "applied": {}}
    state["room"]["human_guidance"] = [
        {"id": "seller-guidance", "role": "seller", "status": "pending", "instruction": "private tactic"}
    ]

    class Adapter:
        def __init__(self, cfg, role, directory):
            self.role = role

        def verify_identity(self):
            pass

        def room(self):
            return copy.deepcopy(state["room"])

        def context(self):
            value = self.room()
            value["role"] = self.role
            value["private_rules"] = context(self.role)["private_rules"]
            value["human_guidance"] = [item for item in value["human_guidance"] if item["role"] == self.role]
            return value

        def claim(self):
            return {"expected_version": state["room"]["version"], "claim_token": "private-claim-token-value"}

        def act(self, payload, key):
            state["actions"].append((copy.deepcopy(payload), key))
            if key in state["applied"]:
                assert state["applied"][key] == payload
                return {"state": state["room"]["state"]}
            state["applied"][key] = copy.deepcopy(payload)
            for item in state["room"]["human_guidance"]:
                if item["id"] == payload.get("guidance_id"):
                    item["status"] = "applied"
            state["room"]["version"] += 1
            if payload["type"] == "propose":
                offer = {
                    "id": "offer-1",
                    "sender": "seller",
                    "terms": {
                        **payload["terms"],
                        "total": 112000,
                        "delivery_charge": 24000,
                    },
                }
                state["room"].update(next_role="buyer", offers=[offer], current_offer=offer)
            else:
                state["room"]["state"] = "awaiting_approval"
            if state["ambiguous"]:
                state["ambiguous"] = False
                raise BattleError("network_error")
            return {"state": state["room"]["state"]}

    class Agent:
        def __init__(self, account, role, client, journal):
            self.role = role

        def decide(self, value):
            state["decisions"].append(self.role)
            return proposal() if self.role == "seller" else Decision(type="accept", offer_id="offer-1")

    journal = Journal(tmp_path, "room-1")
    with pytest.raises(BattleError, match="network_error"):
        run_deepagents(config, journal, adapter=Adapter, participant=Agent)
    assert journal.state["pending"] is not None
    assert journal.state["pending"]["payload"]["guidance_id"] == "seller-guidance"
    restored = Journal(tmp_path, "room-1", resume=True)
    result = run_deepagents(config, restored, adapter=Adapter, participant=Agent)
    assert result["api_state"] == "awaiting_approval"
    assert state["actions"][0] == state["actions"][1]
    assert state["decisions"] == ["seller", "buyer"]
    assert restored.state["pending"] is None
    public = (tmp_path / "buyer.jsonl").read_text()
    assert "private-claim" not in public and "min_unit_price" not in public
    assert "private tactic" not in public and "seller-guidance" not in public
    assert len(state["applied"]) == 2


def test_claim_version_change_never_attaches_stale_decision_to_new_version(tmp_path):
    class Adapter:
        def __init__(self, *args):
            pass

        def verify_identity(self):
            pass

        def room(self):
            return context()

        def context(self):
            return context()

        def claim(self):
            return {"expected_version": 2, "claim_token": "private-claim-token-value"}

        def act(self, payload, key):
            pytest.fail("A stale decision must not be submitted under a newer claimed version")

    class Agent:
        def __init__(self, *args):
            pass

        def decide(self, value):
            return proposal()

    config = {
        "room_id": "room-1",
        "run_dir": str(tmp_path),
        "buyer": {},
        "seller": {},
        "max_model_calls": 12,
        "timeout_seconds": 30,
        "pause_seconds": 0,
    }
    journal = Journal(tmp_path, "room-1")
    with pytest.raises(BattleError, match="state_changed_after_claim"):
        run_deepagents(config, journal, adapter=Adapter, participant=Agent)
    assert journal.state["pending"] is None and journal.state["status"] == "paused"


def guidance_config(tmp_path):
    return {
        "room_id": "room-1",
        "run_dir": str(tmp_path),
        "buyer": {},
        "seller": {},
        "max_model_calls": 4,
        "timeout_seconds": 10,
        "pause_seconds": 0,
    }


def guidance(identifier):
    return {"id": identifier, "role": "seller", "status": "pending", "instruction": "private tactic"}


@pytest.mark.parametrize("bump_version", [True, False])
def test_new_guidance_during_inference_discards_old_decision(tmp_path, bump_version):
    current = context()
    current["human_guidance"] = [guidance("original-guidance")]
    decisions, actions = [], []

    class Adapter:
        def __init__(self, *args):
            pass

        def verify_identity(self):
            pass

        def room(self):
            return copy.deepcopy(current)

        context = room

        def claim(self):
            return {"expected_version": current["version"], "claim_token": "private-claim"}

        def act(self, payload, key):
            actions.append(payload)
            current["state"] = "awaiting_approval"

    class Agent:
        def __init__(self, *args):
            pass

        def decide(self, value):
            decisions.append(value)
            if len(decisions) == 1:
                current["human_guidance"] = [guidance("replacement-guidance")]
                if bump_version:
                    current["version"] += 1
                    current["run"] += 1
                return proposal(11000)
            return proposal(12000)

    result = run_deepagents(
        guidance_config(tmp_path), Journal(tmp_path, "room-1"), adapter=Adapter, participant=Agent
    )
    assert result["api_state"] == "awaiting_approval"
    assert len(decisions) == 2 and len(actions) == 1
    assert actions[0]["guidance_id"] == "replacement-guidance"
    assert actions[0]["terms"]["unit_price"] == 12000
    assert actions[0]["expected_version"] == current["version"]


@pytest.mark.parametrize("rejection", ["guidance_required", "stale_guidance", "stale_version"])
def test_resume_reconciles_rejected_old_guidance_before_new_decision(tmp_path, rejection):
    current = context(version=2)
    current["human_guidance"] = [guidance("new-guidance")]
    actions = []

    class Adapter:
        def __init__(self, *args):
            pass

        def verify_identity(self):
            pass

        def room(self):
            return copy.deepcopy(current)

        context = room

        def claim(self):
            return {"expected_version": 2, "claim_token": "new-private-claim"}

        def act(self, payload, key):
            actions.append((payload, key))
            if key == "old-idempotency-key":
                raise BattleError(rejection)
            current["state"] = "awaiting_approval"

    class Agent:
        def __init__(self, *args):
            pass

        def decide(self, value):
            assert actions[0][1] == "old-idempotency-key"
            assert value["human_guidance"][0]["id"] == "new-guidance"
            return proposal()

    journal = Journal(tmp_path, "room-1")
    old_payload = {
        **proposal().model_dump(exclude_none=True),
        "guidance_id": "old-guidance",
        "expected_version": 1,
        "claim_token": "old-private-claim",
    }
    journal.state["pending"] = {"role": "seller", "payload": old_payload, "key": "old-idempotency-key"}
    journal.save()
    restored = Journal(tmp_path, "room-1", resume=True)
    run_deepagents(guidance_config(tmp_path), restored, adapter=Adapter, participant=Agent)
    assert actions[0] == (old_payload, "old-idempotency-key")
    assert len(actions) == 2 and actions[1][0]["guidance_id"] == "new-guidance"
    assert actions[1][1] != actions[0][1] and restored.state["pending"] is None


def test_deep_follow_processes_human_new_run_preserving_model_budget(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("market_cli.battle.deep.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "market_cli.battle.deep.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    current = context()
    current["state"] = "awaiting_approval"
    reads, actions = [], []

    class Adapter:
        def __init__(self, *args):
            pass

        def verify_identity(self):
            pass

        def room(self):
            reads.append(current["state"])
            if len(reads) == 2:
                current.update(state="negotiating", run=2, version=2, human_guidance=[guidance("new-run")])
            if len(reads) == 4:
                current["state"] = "cancelled"
            return copy.deepcopy(current)

        def context(self):
            return copy.deepcopy(current)

        def claim(self):
            return {"expected_version": 2, "claim_token": "private-claim"}

        def act(self, payload, key):
            actions.append(payload)
            current["state"] = "awaiting_approval"

    class Agent:
        def __init__(self, account, role, client, journal):
            self.journal = journal

        def decide(self, value):
            assert value["run"] == 2 and value["human_guidance"][0]["id"] == "new-run"
            assert self.journal.state["model_calls"] == 3
            self.journal.state["model_calls"] += 1
            self.journal.save()
            return proposal()

    journal = Journal(tmp_path, "room-1")
    journal.state["model_calls"] = 3
    journal.save()
    restored = Journal(tmp_path, "room-1", resume=True)
    result = run_deepagents(
        guidance_config(tmp_path), restored, wait_for_guidance=True, adapter=Adapter, participant=Agent
    )
    assert result["api_state"] == "cancelled" and result["model_calls"] == 4
    assert result["api_run"] == 2 and now[0] == 2
    assert len(actions) == 1 and actions[0]["guidance_id"] == "new-run"


def test_deep_follow_waiting_has_wall_clock_bound_and_no_model_calls(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("market_cli.battle.deep.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "market_cli.battle.deep.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )

    class Adapter:
        def __init__(self, *args):
            pass

        def verify_identity(self):
            pass

        def room(self):
            return {**context(), "state": "awaiting_approval"}

    class Agent:
        def __init__(self, *args):
            pass

        def decide(self, value):
            pytest.fail("Waiting for human guidance must not spend model calls")

    journal = Journal(tmp_path, "room-1")
    with pytest.raises(BattleError, match="battle_time_limit_reached"):
        run_deepagents(
            guidance_config(tmp_path), journal, wait_for_guidance=True, adapter=Adapter, participant=Agent
        )
    assert now[0] == 10 and journal.state["model_calls"] == 0
    assert journal.state["status"] == "paused"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "message", "message": "What matters most?", "purpose": "question"},
        {
            "type": "message",
            "message": "Deposit matters most.",
            "purpose": "answer",
            "reply_to_action_id": "question-id",
        },
        {"type": "request_human_review", "human_question": "Privately, is a lower deposit worth €20?"},
    ],
)
def test_conversation_decision_shapes_match_protocol(payload):
    assert Decision.model_validate(payload).model_dump(exclude_none=True) == payload


@pytest.mark.parametrize(
    "extra",
    [
        {"message": "A public explanation"},
        {"purpose": "explanation"},
        {"image_ids": ["image-id"]},
        {"human_question": "Cannot attach a private question to an offer"},
        {"message": "Evidence", "purpose": "explanation", "image_ids": ["same", "same"]},
    ],
)
def test_offer_decision_rejects_malformed_or_private_message_fields(extra):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Decision.model_validate({**proposal().model_dump(exclude_none=True), **extra})


def test_legacy_decisions_do_not_add_new_defaults_to_retry_payload():
    original = {"type": "decline"}
    assert Decision.model_validate(original).model_dump(exclude_none=True) == original
    old_offer = proposal().model_dump(exclude_none=True)
    assert set(old_offer) == {"type", "terms"}
    revised = Decision.model_validate(
        {
            **old_offer,
            "type": "revise",
            "message": "Smaller deposit at a higher unit price.",
            "purpose": "explanation",
        }
    )
    assert revised.type == "revise" and revised.purpose == "explanation"


def test_private_memory_retains_complete_recent_turns_without_repeated_images():
    from market_cli.battle.deep import bounded_memory

    messages = []
    for turn in range(8):
        messages.extend(
            [
                {
                    "type": "human",
                    "data": {
                        "content": [
                            {"type": "text", "text": f"private turn {turn}"},
                            {"type": "image", "base64": "heavy-photo"},
                        ]
                    },
                },
                {"type": "ai", "data": {"content": "", "tool_calls": [{"id": f"tool-{turn}"}]}},
                {"type": "tool", "data": {"content": "fact", "tool_call_id": f"tool-{turn}"}},
            ]
        )
    retained = bounded_memory(messages)
    assert len(retained) == 18
    assert retained[0]["type"] == "human" and retained[0]["data"]["content"][0]["text"] == "private turn 2"
    assert retained[2]["data"]["tool_call_id"] == "tool-2"
    assert "heavy-photo" not in json.dumps(retained)


def test_deep_controller_exchanges_dialogue_revises_then_stops_for_humans(tmp_path):
    current = context()
    current["allowed_actions"] += ["message", "revise"]
    actions = []
    dialogue = [
        Decision(
            type="message", message="Which payment tradeoff matters to your customer?", purpose="question"
        ),
        Decision(
            type="message",
            message="A lower upfront deposit matters most.",
            purpose="answer",
            reply_to_action_id="action-1",
        ),
        proposal(11500).model_copy(
            update={"message": "€115 per chair with a 25% deposit.", "purpose": "explanation"}
        ),
        Decision(
            type="message",
            message="Could 20% deposit be worth a slightly higher price?",
            purpose="tradeoff",
            reply_to_action_id="action-3",
        ),
        proposal(11750).model_copy(
            update={
                "type": "revise",
                "terms": proposal(11750).terms.model_copy(update={"deposit_percent": 20}),
                "message": "€117.50 with a 20% deposit lowers the upfront payment.",
                "purpose": "explanation",
            }
        ),
        Decision(type="accept", offer_id="offer-5"),
    ]

    class Adapter:
        def __init__(self, cfg, role, directory):
            self.role = role

        def verify_identity(self):
            pass

        def room(self):
            return copy.deepcopy(current)

        def context(self):
            value = self.room()
            value["role"] = self.role
            value["private_rules"] = context(self.role)["private_rules"]
            value["private_brief"] = {"version": 1, "goals": f"{self.role}-private-goal", "requirements": []}
            value["human_questions"] = []
            return value

        def claim(self):
            return {"expected_version": current["version"], "claim_token": "private-claim-token"}

        def act(self, payload, key):
            actions.append(payload)
            public = {
                field: payload[field]
                for field in ("message", "purpose", "reply_to_action_id", "image_ids")
                if field in payload
            }
            if payload["type"] in ("propose", "revise"):
                offer = {
                    "id": f"offer-{len(actions)}",
                    "sender": self.role,
                    "terms": {
                        **payload["terms"],
                        "total": payload["terms"]["unit_price"] * 8 + 24000,
                        "delivery_charge": 24000,
                    },
                }
                current["offers"].append(offer)
                current["current_offer"] = offer
                public["offer_id"] = offer["id"]
            current["activity"].append(
                {
                    "id": f"action-{len(actions)}",
                    "role": self.role,
                    "type": payload["type"],
                    "data": public,
                    "at": f"2026-10-10T10:00:0{len(actions)}Z",
                }
            )
            current["version"] += 1
            current["next_role"] = "seller" if self.role == "buyer" else "buyer"
            if payload["type"] == "accept":
                current["state"] = "awaiting_approval"

    class Agent:
        def __init__(self, account, role, client, journal):
            self.role, self.journal = role, journal

        def decide(self, value):
            assert value["private_brief"]["goals"] == f"{self.role}-private-goal"
            self.journal.state["model_calls"] += 1
            return dialogue[len(actions)]

    journal = Journal(tmp_path, "room-1")
    settings = {**guidance_config(tmp_path), "max_model_calls": 8}
    result = run_deepagents(settings, journal, adapter=Adapter, participant=Agent)
    assert result["api_state"] == "awaiting_approval" and result["model_calls"] == 6
    assert [action["type"] for action in actions] == [
        "message",
        "message",
        "propose",
        "message",
        "revise",
        "accept",
    ]
    text = (tmp_path / "buyer.jsonl").read_text()
    assert (
        "lowers the upfront payment" in text
        and "seller-private-goal" not in text
        and "buyer-private-goal" not in text
    )
    assert result["current_terms"]["total"] == 118000
    assert result["current_terms"]["deposit_percent"] == 20


def test_private_owner_question_stops_without_leaking_to_public_battle(tmp_path):
    state = context()
    question = "PRIVATE: prioritize the smaller deposit or lower total?"
    submitted = []

    class Adapter:
        def __init__(self, *args):
            pass

        def verify_identity(self):
            pass

        def room(self):
            return copy.deepcopy(state)

        context = room

        def claim(self):
            return {"expected_version": state["version"], "claim_token": "private-claim"}

        def act(self, payload, key):
            submitted.append(payload)
            state["human_questions"] = [{"question": payload["human_question"], "status": "pending"}]
            state["activity"] = [
                {"id": "review", "role": "seller", "type": "request_human_review", "data": {}}
            ]
            state["state"] = "needs_human_review"

    class Agent:
        def __init__(self, *args):
            pass

        def decide(self, value):
            return Decision(type="request_human_review", human_question=question)

    journal = Journal(tmp_path, "room-1")
    result = run_deepagents(guidance_config(tmp_path), journal, adapter=Adapter, participant=Agent)
    assert result["api_state"] == "needs_human_review"
    assert submitted[0]["human_question"] == question and "message" not in submitted[0]
    assert "PRIVATE" not in (tmp_path / "buyer.jsonl").read_text()
