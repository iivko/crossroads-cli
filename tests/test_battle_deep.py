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
        p.decide(own_context)
    buyer_text = captures[0]["messages"][-1].content[0]["text"]
    seller_text = captures[1]["messages"][-1].content[0]["text"]
    assert "max_total" in buyer_text and "min_unit_price" not in buyer_text
    assert "min_unit_price" in seller_text and "max_total" not in seller_text
    assert "buyer-private-tactic" in buyer_text and "seller-private-tactic" not in buyer_text
    assert "seller-private-tactic" in seller_text and "buyer-private-tactic" not in seller_text
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
