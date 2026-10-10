"""Provider-neutral battle monitor and durable, sanitized terminal events."""

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..conversation import public_messages, terminal_text
from .io import BattleError, Marketplace, private_json, read_private

TERMINAL = {"awaiting_approval", "ordered", "declined", "cancelled", "expired", "needs_human_review"}
FINAL = {"ordered", "cancelled"}
ACTIVE = {"negotiating", "needs_revision"}
MESSAGES = {
    "ready": "Both participants are ready. Waiting for independent agents to negotiate.",
    "propose": "The participant has proposed new terms.",
    "counter": "A new counteroffer is on the table.",
    "revise": "The participant has revised its own offer.",
    "message": "The participant shared a commercial message.",
    "accept": "Agreement reached. Both humans must approve the exact terms.",
    "decline": "The participant declined. No order has been created.",
    "request_human_review": "The participant requested human review.",
    "request_clarification": "The participant asked for clarification.",
    "answer_clarification": "The participant supplied catalogue evidence.",
    "paused": "Battle paused. Resume to reconcile the current API state.",
    "timeout": "The observer time limit was reached. Participants remain independent.",
    "thinking": "The participant is inspecting its context and product images.",
    "finished": "Negotiation stopped. Review the final state in the dashboard.",
    "waiting_for_guidance": "Waiting for a human request or final approval within the time limit.",
    "human_requested_negotiation": "A human requested another negotiation attempt.",
    "business_brief_updated": "A human updated its negotiation needs and started another attempt.",
}


def should_finish(room_state, wait_for_guidance):
    return room_state in (FINAL if wait_for_guidance else TERMINAL)


def pending_guidance_id(context):
    """The API returns only this role's guidance, newest first."""
    return next(
        (item["id"] for item in context.get("human_guidance", []) if item["status"] == "pending"),
        None,
    )


def public_terms(terms):
    """Positive allowlist: never serialize request, policy, model text or error details."""
    result = {}
    for key in ("unit_price", "quantity", "delivery_charge", "total", "deposit_percent"):
        if type(terms.get(key)) is int:
            result[key] = terms[key]
    date = terms.get("delivery_date")
    if isinstance(date, str):
        try:
            result["delivery_date"] = datetime.strptime(date, "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    return result


class Journal:
    def __init__(self, directory, room_id, resume=False):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.stat().st_mode & 0o077:
            raise BattleError("run_directory_must_be_private")
        self.path = self.directory / "state.json"
        if self.path.exists():
            if not resume:
                raise BattleError("battle_exists_use_resume")
            self.state = read_private(self.path)
            if self.state["room_id"] != room_id:
                raise BattleError("resume_room_mismatch")
        else:
            self.state = {
                "room_id": room_id,
                "status": "starting",
                "sequence": 0,
                "seen_activity": [],
                "seen_offers": [],
                "model_calls": 0,
                "pending": None,
            }
            self.save()

    def save(self):
        private_json(self.path, self.state)

    def emit(self, event, role="system", *, terms=None, room_state=None, dialogue=None):
        if event not in MESSAGES or role not in ("buyer", "seller", "system"):
            raise BattleError("invalid_public_event")
        self.state["sequence"] += 1
        record = {
            "sequence": self.state["sequence"],
            "at": datetime.now(UTC).isoformat(),
            "role": role,
            "event": event,
            "message": MESSAGES[event],
        }
        if terms:
            record["terms"] = public_terms(terms)
        if dialogue and event in {"message", "propose", "counter", "revise"}:
            # Only the explicitly public transcript projection reaches this path.
            record["dialogue"] = {"message": terminal_text(dialogue["message"])}
            if dialogue.get("purpose") in {"question", "answer", "tradeoff", "explanation"}:
                record["dialogue"]["purpose"] = dialogue["purpose"]
            if isinstance(dialogue.get("reply_to_action_id"), str):
                record["dialogue"]["reply_to_action_id"] = terminal_text(dialogue["reply_to_action_id"], 50)
            if isinstance(dialogue.get("image_ids"), list):
                record["dialogue"]["image_ids"] = [
                    terminal_text(value, 50) for value in dialogue["image_ids"][:5]
                ]
        if room_state in TERMINAL | ACTIVE:
            record["state"] = room_state
        for side in ("buyer", "seller"):
            fd = os.open(self.directory / f"{side}.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write(json.dumps(record) + "\n")
        self.save()

    def observe(self, room):
        self.state["api_state"] = room["state"]
        self.state["api_version"] = room["version"]
        self.state["api_run"] = room.get("run")
        self.state["next_role"] = room["next_role"]
        activities = room.get("activity", [])
        dialogue = {item["id"]: item for item in public_messages(room)}
        offer_actions = {
            item["data"]["offer_id"]: item
            for item in activities
            if item["type"] in ("propose", "counter", "revise") and item.get("data", {}).get("offer_id")
        }
        events = []
        # A new run can start with either role proposing. Its persisted action is
        # authoritative; offer history alone cannot distinguish propose/counter.
        for offer in room.get("offers", []):
            if offer["id"] not in self.state["seen_offers"]:
                action = offer_actions.get(offer["id"])
                at = action.get("at") if action else None
                events.append((at or offer.get("created_at"), 1, "offer", offer, action))
        for activity in activities:
            if activity["id"] not in self.state["seen_activity"]:
                priority = (
                    0 if activity["type"] in {"human_requested_negotiation", "business_brief_updated"} else 2
                )
                events.append((activity.get("at"), priority, "activity", activity, None))

        def chronological(event):
            try:
                at = datetime.fromisoformat(event[0])
                at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
            except (TypeError, ValueError):
                at = datetime.min.replace(tzinfo=UTC)
            return at, event[1]

        for _, _, kind, item, action in sorted(events, key=chronological):
            if kind == "offer":
                if item["id"] in self.state["seen_offers"]:
                    continue
                fallback = "propose" if not self.state["seen_offers"] else "counter"
                event = action["type"] if action else fallback
                self.state["seen_offers"].append(item["id"])
                # Each offer carries its sender; monitor credentials do not imply authorship.
                self.emit(
                    event,
                    item["sender"],
                    terms=item["terms"],
                    room_state=room["state"],
                    dialogue=dialogue.get(action["id"]) if action else None,
                )
            else:
                if item["id"] in self.state["seen_activity"]:
                    continue
                self.state["seen_activity"].append(item["id"])
                if item["type"] in MESSAGES and item["type"] not in ("propose", "counter", "revise"):
                    self.emit(
                        item["type"],
                        item["role"],
                        room_state=room["state"],
                        dialogue=dialogue.get(item["id"]),
                    )
        current = room.get("current_offer")
        if current:
            self.state["current_terms"] = public_terms(current["terms"])
        self.save()

    def summary(self):
        return {
            k: self.state[k]
            for k in (
                "room_id",
                "status",
                "api_state",
                "api_version",
                "api_run",
                "next_role",
                "current_terms",
                "model_calls",
            )
            if k in self.state
        }


@contextmanager
def run_lock(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "controller.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BattleError("battle_controller_already_running") from None
        yield
    finally:
        os.close(fd)


def run_external(
    config, journal, *, wait_for_guidance=False, adapter=Marketplace, sleep=time.sleep, clock=time.monotonic
):
    """Observe real independent participants; never fabricate offers or model decisions."""
    clients = {role: adapter(config, role, journal.directory) for role in ("buyer", "seller")}
    for client in clients.values():
        client.verify_identity()
    deadline = clock() + config["timeout_seconds"]
    journal.state["status"] = "observing"
    journal.emit("ready")
    try:
        while clock() < deadline:
            room = clients["buyer"].room()
            journal.observe(room)
            if should_finish(room["state"], wait_for_guidance):
                journal.state["status"] = "finished"
                journal.emit("finished", room_state=room["state"])
                return journal.summary()
            if room["state"] in TERMINAL:
                if journal.state["status"] != "waiting_for_guidance":
                    journal.state["status"] = "waiting_for_guidance"
                    journal.emit("waiting_for_guidance", room_state=room["state"])
            else:
                journal.state["status"] = "observing"
                journal.save()
            sleep(min(1, max(0, deadline - clock())))
        journal.state["status"] = "timeout"
        journal.emit("timeout")
        return journal.summary()
    except BaseException:
        journal.state["status"] = "paused"
        journal.emit("paused")
        raise
