import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx


class CLIError(Exception):
    def __init__(self, code, message, exit_code=1, details=None):
        self.code, self.message, self.exit_code, self.details = code, message, exit_code, details


def config_path():
    return Path(
        os.environ.get(
            "MARKET_CONFIG",
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "crossroads" / "config.json",
        )
    )


def read_config():
    path = config_path()
    if not path.exists():
        return {"profiles": {}}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise CLIError("config_invalid", "Configuration cannot be read; inspect MARKET_CONFIG", 2)


def save_profile(name, values):
    data = read_config()
    data.setdefault("profiles", {}).setdefault(name, {}).update(values)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass
class Client:
    base_url: str
    token: str | None = None
    workspace: str | None = None
    key: str | None = None

    def __post_init__(self):
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise CLIError(
                "invalid_url", "Use an HTTP(S) API URL without embedded credentials, query or fragment", 2
            )
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1", "api"):
            raise CLIError("insecure_url", "Remote API connections require HTTPS", 2)
        self.base_url = self.base_url.rstrip("/")
        if not self.base_url.endswith("/api/v1"):
            self.base_url += "/api/v1"

    def call(
        self, method, path, data=None, params=None, files=None, authenticated=True, binary=False, key=None
    ):
        headers = {}
        if authenticated:
            if not self.token:
                raise CLIError("unauthenticated", "Use market auth login or set MARKET_TOKEN", 3)
            headers["Authorization"] = "Bearer " + self.token
        if self.workspace:
            headers["X-Workspace-ID"] = self.workspace
        retry_key = key or self.key or str(uuid4())
        if method != "GET":
            headers["Idempotency-Key"] = retry_key
        try:
            with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as transport:
                response = transport.request(
                    method,
                    self.base_url + path,
                    params=params,
                    headers=headers,
                    json=data if files is None else None,
                    data=data if files else None,
                    files=files,
                )
        except httpx.RequestError:
            raise CLIError(
                "network_error",
                "API request failed. Retry mutations with the same idempotency key.",
                6,
                {"idempotency_key": retry_key} if method != "GET" else None,
            )
        if response.status_code >= 300:
            try:
                error = response.json().get("error", {})
            except (ValueError, AttributeError):
                error = {}
            status = response.status_code
            raise CLIError(
                error.get("code", "http_error"),
                error.get("message", f"API returned HTTP {status}"),
                {401: 3, 403: 4, 409: 5, 422: 2}.get(status, 1),
                error.get("details"),
            )
        return response.content if binary else response.json()
