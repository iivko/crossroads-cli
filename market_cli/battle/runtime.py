"""Provider-neutral battle monitor and durable, sanitized terminal events."""

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .io import BattleError, Marketplace, private_json, read_private

TERMINAL = {"awaiting_approval", "ordered", "declined", "cancelled", "expired", "needs_human_review"}
ACTIVE = {"negotiating", "needs_revision"}
MESSAGES = {
    "ready": "Both participants are ready. Waiting for independent agents to negotiate.",
    "propose": "The seller has opened negotiations.",
    "counter": "A new counteroffer is on the table.",
    "accept": "Agreement reached. Both humans must approve the exact terms.",
    "decline": "The participant declined. No order has been created.",
    "request_human_review": "The participant requested human review.",
    "request_clarification": "The participant asked for clarification.",
    "answer_clarification": "The participant supplied catalogue evidence.",
    "paused": "Battle paused. Resume to reconcile the current API state.",
    "timeout": "The observer time limit was reached. Participants remain independent.",
    "thinking": "The participant is inspecting its context and product images.",
    "finished": "Negotiation stopped. Review the final state in the dashboard.",
}


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

    def emit(self, event, role="system", *, terms=None, room_state=None):
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
        self.state["next_role"] = room["next_role"]
        # Each offer carries its actual sender. Never infer it from monitor credentials.
        for offer in room.get("offers", []):
            if offer["id"] not in self.state["seen_offers"]:
                event = "propose" if not self.state["seen_offers"] else "counter"
                self.state["seen_offers"].append(offer["id"])
                self.emit(event, offer["sender"], terms=offer["terms"], room_state=room["state"])
        for activity in room.get("activity", []):
            if activity["id"] in self.state["seen_activity"]:
                continue
            self.state["seen_activity"].append(activity["id"])
            if activity["type"] in MESSAGES and activity["type"] not in ("propose", "counter"):
                self.emit(activity["type"], activity["role"], room_state=room["state"])
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


def run_external(config, journal, *, adapter=Marketplace, sleep=time.sleep, clock=time.monotonic):
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
            if room["state"] in TERMINAL:
                journal.state["status"] = "finished"
                journal.emit("finished", room_state=room["state"])
                return journal.summary()
            sleep(min(1, max(0, deadline - clock())))
        journal.state["status"] = "timeout"
        journal.emit("timeout")
        return journal.summary()
    except BaseException:
        journal.state["status"] = "paused"
        journal.emit("paused")
        raise
