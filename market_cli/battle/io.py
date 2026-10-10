"""Private persistence and an intentionally narrow subprocess CLI adapter."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4


class BattleError(Exception):
    """Only a safe code crosses the boundary into the public terminal."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_private(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise BattleError("private_file_must_be_mode_0600")
    return json.loads(path.read_text())


def load_config(path):
    config = read_private(path)
    if not isinstance(config, dict) or not config.get("room_id") or not config.get("run_dir"):
        raise BattleError("invalid_battle_config")
    from ..client import Client

    Client(config.get("api_url", "http://127.0.0.1:8100"))
    for role in ("buyer", "seller"):
        account = config.get(role, {})
        if not account.get("workspace_id") or not account.get("token"):
            raise BattleError("missing_role_credentials")
    if config["buyer"]["token"] == config["seller"]["token"]:
        raise BattleError("roles_require_distinct_credentials")
    for key, default, low, high in (
        ("timeout_seconds", 900, 10, 3600),
        ("max_model_calls", 32, 1, 100),
        ("pause_seconds", 3, 0, 30),
    ):
        value = config.setdefault(key, default)
        if type(value) not in (int, float) or not low <= value <= high:
            raise BattleError("invalid_battle_limits")
    return config


class Marketplace:
    """No shell and no caller-supplied commands; only these named methods exist."""

    def __init__(self, config, role, run_dir, timeout=40):
        self.role, self.room_id = role, config["room_id"]
        self.directory = Path(run_dir) / "private" / role
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.timeout = timeout
        account = config[role]
        # Never inherit the other party's MARKET_* credentials or a saved profile.
        self.env = {
            k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT"}
        }
        self.env.update(
            MARKET_API_URL=config.get("api_url", "http://127.0.0.1:8100"),
            MARKET_TOKEN=account["token"],
            MARKET_WORKSPACE=account["workspace_id"],
            MARKET_CONFIG=str(self.directory / "empty-profile.json"),
        )
        self.workspace_id = account["workspace_id"]

    def _run(self, args):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "market_cli.main", *args, "--json"],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            raise BattleError("network_error") from None
        try:
            value = json.loads(result.stderr if result.returncode else result.stdout)
        except (ValueError, TypeError):
            raise BattleError("cli_invalid_response") from None
        if result.returncode:
            code = value.get("error", {}).get("code", "cli_error")
            if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
                code = "cli_error"
            raise BattleError(code)
        return value

    def verify_identity(self):
        identity = self._run(["auth", "whoami"])
        credential, workspace = identity["credential"], identity["workspace"]
        required = {"catalogue:read", "images:read", "negotiations:read", "negotiations:act"}
        allowed = required | {"orders:read"} | ({"requests:write"} if self.role == "buyer" else set())
        if (
            workspace["id"] != self.workspace_id
            or workspace["role"] != self.role
            or credential["kind"] != "agent"
            or not required <= set(credential["scopes"])
            or not set(credential["scopes"]) <= allowed
        ):
            raise BattleError("restricted_agent_credentials_required")

    def context(self):
        value = self._run(["negotiations", "context", self.room_id])
        if value.get("role") != self.role:
            raise BattleError("context_role_mismatch")
        return value

    def room(self):
        return self._run(["negotiations", "show", self.room_id])

    def product(self, product_id):
        return self._run(["products", "show", product_id])

    def images(self, product_id):
        manifest = self._run(
            [
                "products",
                "images",
                "read",
                product_id,
                "--all",
                "--output-dir",
                str(self.directory / "images"),
            ]
        )
        blocks = []
        import base64

        for item in manifest["images"]:
            path = Path(item["path"]).resolve()
            if not path.is_relative_to((self.directory / "images").resolve()):
                raise BattleError("image_path_outside_role_directory")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["sha256"] or len(raw) > 10 * 1024 * 1024:
                raise BattleError("image_checksum_mismatch")
            if item["mime"] not in ("image/jpeg", "image/png", "image/webp"):
                raise BattleError("unsupported_image_type")
            blocks.append(
                {"type": "image", "base64": base64.b64encode(raw).decode(), "mime_type": item["mime"]}
            )
        if not blocks:
            raise BattleError("product_requires_ready_image")
        return blocks

    def claim(self):
        return self._run(["agent", "claim", self.room_id, "--idempotency-key", str(uuid4())])

    def act(self, payload, key):
        path = self.directory / "pending-action.json"
        private_json(path, payload)
        return self._run(["negotiations", "act", self.room_id, "--file", str(path), "--idempotency-key", key])
