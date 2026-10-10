"""Typer commands with the same JSON contract for humans and external LLM agents."""

import hashlib
import inspect
import json
import os
import re
import sys
import time
import webbrowser
from functools import wraps
from pathlib import Path
from uuid import uuid4

import typer

from .battle.commands import app as battle
from .client import Client, CLIError, read_config, save_profile

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Crossroads: provider-neutral B2B marketplace tools. Amounts are integer EUR cents.",
)
auth = typer.Typer(help="User login and scoped agent tokens.")
workspace = typer.Typer(help="List and select workspaces you belong to.")
credentials = typer.Typer(help="Owner-only scoped agent credential administration.")
products = typer.Typer(help="Read catalogue; sellers manage only their own products.")
images = typer.Typer(help="Upload, download and organize real product image files.")
stock = typer.Typer(help="Seller inventory management.")
rules = typer.Typer(help="Inspect private rules; owners can publish policy versions.")
requests = typer.Typer(help="Buyer-only purchase requests and matching.")
negotiations = typer.Typer(help="Role-scoped context and structured commercial actions.")
agent = typer.Typer(help="Instructions, work discovery and turn claims for any agent runtime.")
approvals = typer.Typer(help="Read approval status or open human review. CLI tokens cannot approve.")
orders = typer.Typer(help="Inspect your workspace's confirmed orders and evidence.")
for name, group in [
    ("battle", battle),
    ("auth", auth),
    ("workspace", workspace),
    ("credentials", credentials),
    ("products", products),
    ("rules", rules),
    ("requests", requests),
    ("negotiations", negotiations),
    ("agent", agent),
    ("approvals", approvals),
    ("orders", orders),
]:
    app.add_typer(group, name=name)
products.add_typer(images, name="images")
products.add_typer(stock, name="stock")
state = {}


def output(value):
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False))


def command(group, name=None):
    """All leaf commands accept --json and an explicit retry key, at any leaf position."""

    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            kwargs.pop("json_output", None)
            key = kwargs.pop("idempotency_key", None)
            state["client"].key = key
            try:
                result = fn(*args, **kwargs)
                if result is not None:
                    output(result)
            except CLIError as exc:
                typer.echo(
                    json.dumps({"error": {"code": exc.code, "message": exc.message, "details": exc.details}}),
                    err=True,
                )
                raise typer.Exit(exc.exit_code)
            except (OSError, ValueError) as exc:
                typer.echo(
                    json.dumps({"error": {"code": "local_input_error", "message": str(exc)}}), err=True
                )
                raise typer.Exit(2)

        sig = inspect.signature(fn)
        wrapper.__signature__ = sig.replace(
            parameters=[
                *sig.parameters.values(),
                inspect.Parameter(
                    "json_output",
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=bool,
                    default=typer.Option(False, "--json", help="Emit JSON (also the default)."),
                ),
                inspect.Parameter(
                    "idempotency_key",
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=str | None,
                    default=typer.Option(
                        None, "--idempotency-key", help="Reuse this key when retrying the same mutation."
                    ),
                ),
            ]
        )
        group.command(name)(wrapper)
        return wrapper

    return decorate


@app.callback()
def configure(
    profile: str = typer.Option("default", envvar="MARKET_PROFILE"),
    base_url: str | None = typer.Option(None, envvar="MARKET_API_URL"),
    workspace_id: str | None = typer.Option(None, "--workspace", envvar="MARKET_WORKSPACE"),
):
    """Global options precede command groups; leaf commands also accept --json."""
    try:
        saved = read_config().get("profiles", {}).get(profile, {})
        state.update(
            profile=profile,
            client=Client(
                base_url or saved.get("base_url", "http://127.0.0.1:8100"),
                os.environ.get("MARKET_TOKEN") or saved.get("token"),
                workspace_id or saved.get("workspace"),
            ),
        )
    except CLIError as exc:
        typer.echo(json.dumps({"error": {"code": exc.code, "message": exc.message}}), err=True)
        raise typer.Exit(exc.exit_code)


def call(method, path, **kwargs):
    return state["client"].call(method, path, **kwargs)


def document(path):
    value = json.loads(path.read_text() if str(path) != "-" else sys.stdin.read())
    if not isinstance(value, dict):
        raise CLIError("invalid_document", "Expected a JSON object", 2)
    return value


@command(auth, "login")
def login(email: str = typer.Option(...), password_stdin: bool = typer.Option(False, "--password-stdin")):
    """Sign in as a user; store a CLI token with file mode 0600, never a browser approval session."""
    password = sys.stdin.readline().rstrip("\r\n") if password_stdin else os.environ.get("MARKET_PASSWORD")
    if not password:
        if not sys.stdin.isatty():
            raise CLIError(
                "password_required", "Use --password-stdin or MARKET_PASSWORD in noninteractive sessions", 2
            )
        password = typer.prompt("Password", hide_input=True)
    result = call("POST", "/auth/token", data={"email": email, "password": password}, authenticated=False)
    c = state["client"]
    c.token = result["token"]
    save_profile(state["profile"], {"base_url": c.base_url, "token": c.token})
    return call("GET", "/auth/me")


@command(auth, "use-token")
def use_token(token_stdin: bool = typer.Option(False, "--token-stdin")):
    """Use a preissued agent token. Prefer MARKET_TOKEN to avoid persisting it."""
    token = sys.stdin.readline().strip() if token_stdin else os.environ.get("MARKET_TOKEN")
    if not token:
        raise CLIError("token_required", "Supply MARKET_TOKEN or --token-stdin", 2)
    c = state["client"]
    c.token = token
    result = call("GET", "/auth/me")
    save_profile(
        state["profile"], {"base_url": c.base_url, "token": token, "workspace": result["workspace"]["id"]}
    )
    return result


@command(auth, "whoami")
def whoami():
    return call("GET", "/auth/me")


@command(auth, "logout")
def logout():
    result = call("POST", "/auth/logout")
    save_profile(state["profile"], {"token": None, "workspace": None})
    return result


@command(workspace, "list")
def workspace_list():
    return call("GET", "/workspaces")


@command(workspace, "use")
def workspace_use(workspace_id: str):
    c = state["client"]
    c.workspace = workspace_id
    result = call("GET", "/auth/me")
    save_profile(state["profile"], {"workspace": workspace_id})
    return result


@command(credentials, "list")
def credentials_list():
    return call("GET", "/credentials")


@command(credentials, "create")
def credentials_create(file: Path = typer.Option(..., "--file")):
    """Issue a restricted agent credential; output includes its secret token exactly once."""
    return call("POST", "/credentials", data=document(file))


@command(credentials, "revoke")
def credentials_revoke(credential_id: str):
    return call("DELETE", "/credentials/" + credential_id)


@command(products, "list")
def products_list(
    q: str = "",
    own: bool = False,
    material: str | None = None,
    destination: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    return call("GET", "/products", params={k: v for k, v in locals().items() if v is not None})


@command(products, "show")
def products_show(product_id: str):
    return call("GET", f"/products/{product_id}")


@command(products, "create")
def products_create(file: Path = typer.Option(..., "--file")):
    return call("POST", "/products", data=document(file))


@command(products, "update")
def products_update(product_id: str, file: Path = typer.Option(..., "--file")):
    """Apply a partial JSON edit with the fetched product version, or explicit expected_version."""
    current = call("GET", f"/products/{product_id}")
    fields = [
        "name",
        "description",
        "sku",
        "category",
        "attributes",
        "unit_price",
        "delivery_charge",
        "lead_days",
        "destinations",
        "stock",
        "published",
    ]
    data = {k: current[k] for k in fields}
    data["expected_version"] = current["version"]
    data.update(document(file))
    return call("PUT", f"/products/{product_id}", data=data)


@command(products, "history")
def products_history(product_id: str):
    return call("GET", f"/products/{product_id}/history")


@command(stock, "set")
def stock_set(product_id: str, quantity: int, expected_version: int | None = None):
    version = (
        expected_version
        if expected_version is not None
        else call("GET", f"/products/{product_id}")["version"]
    )
    return call("PUT", f"/products/{product_id}/stock", data={"stock": quantity, "expected_version": version})


@command(images, "upload")
def images_upload(product_id: str, files: list[Path] = typer.Argument(...), caption: str = ""):
    """Upload files inside MARKET_MEDIA_ROOT (default: current directory). Each file is independent."""
    root = Path(os.environ.get("MARKET_MEDIA_ROOT", Path.cwd())).resolve()
    results, failures = [], []
    base_key = state["client"].key or str(uuid4())
    for path in files:
        real = path.resolve()
        if not real.is_relative_to(root) or not real.is_file():
            raise CLIError(
                "media_path_forbidden", "Uploads must be regular files inside MARKET_MEDIA_ROOT", 2
            )
        if real.stat().st_size > 10 * 1024 * 1024:
            raise CLIError("image_too_large", "Image exceeds 10 MB", 2)
        content = real.read_bytes()
        key = hashlib.sha256(
            (base_key + str(real) + hashlib.sha256(content).hexdigest()).encode()
        ).hexdigest()
        try:
            result = call(
                "POST",
                f"/products/{product_id}/images",
                data={"caption": caption},
                files={"file": (real.name, content)},
                key=key,
            )
            results.append(result)
        except CLIError as exc:
            failures.append(
                {"file": str(path), "code": exc.code, "message": exc.message, "idempotency_key": key}
            )
    if failures:
        raise CLIError(
            "partial_upload",
            "Some images were not uploaded. Successful uploads are retained.",
            1,
            {"uploaded": results, "failed": failures},
        )
    return {"images": results, "status": "processing"}


@command(images, "list")
def images_list(product_id: str):
    return call("GET", f"/products/{product_id}/images")


@command(images, "read")
def images_read(
    product_id: str,
    all_images: bool = typer.Option(False, "--all"),
    image_id: str | None = None,
    output_dir: Path = Path("./product-images"),
):
    """Download actual image files and emit their paths for a vision-capable agent runtime."""
    items = call("GET", f"/products/{product_id}/images")
    ready = [i for i in items if i["status"] == "ready"]
    if image_id:
        ready = [i for i in ready if i["id"] == image_id]
    elif not all_images:
        ready = ready[:1]
    if not ready:
        raise CLIError("no_ready_images", "No matching ready images; uploads may still be processing", 5)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifests = []
    for item in ready:
        if not re.fullmatch(r"[A-Za-z0-9-]+", item["id"]):
            raise CLIError("invalid_image_id", "Server returned an invalid image ID", 1)
        content = call("GET", f"/images/{item['id']}/content", binary=True)
        checksum = hashlib.sha256(content).hexdigest()
        if checksum != item["sha256"]:
            raise CLIError("checksum_mismatch", "Downloaded image failed checksum verification", 1)
        path = output_dir / (item["id"] + ".jpg")
        if path.is_symlink():
            raise CLIError("unsafe_output_path", "Image destination cannot be a symlink", 2)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise CLIError(
                    "file_exists",
                    "Destination exists with different content; use another output directory",
                    2,
                )
        else:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(content)
        manifests.append(
            {
                "image_id": item["id"],
                "path": str(path.resolve()),
                "mime": item["mime"],
                "sha256": checksum,
                "caption": item["caption"],
            }
        )
    return {
        "product_id": product_id,
        "images": manifests,
        "next_step": "Attach these actual local files to your runtime's image-reading tool. The manifest itself does not provide vision.",
    }


@command(images, "open")
def images_open(image_id: str, output_dir: Path = Path("./product-images")):
    if not re.fullmatch(r"[A-Za-z0-9-]+", image_id):
        raise CLIError("invalid_image_id", "Invalid image ID", 2)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / (image_id + ".jpg")
    if path.exists() or path.is_symlink():
        raise CLIError("file_exists", "Use another output directory; image destination already exists", 2)
    content = call("GET", f"/images/{image_id}/content", binary=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    webbrowser.open(path.resolve().as_uri())
    return {"path": str(path.resolve())}


@command(images, "cover")
def images_cover(product_id: str, image_id: str, expected_version: int | None = None):
    version = (
        expected_version
        if expected_version is not None
        else call("GET", f"/products/{product_id}")["version"]
    )
    return call(
        "PUT",
        f"/products/{product_id}/images/cover",
        data={"image_id": image_id, "expected_version": version},
    )


@command(images, "reorder")
def images_reorder(product_id: str, file: Path = typer.Option(..., "--file")):
    data = document(file)
    data.setdefault("expected_version", call("GET", f"/products/{product_id}")["version"])
    return call("PUT", f"/products/{product_id}/images/order", data=data)


@command(images, "remove")
def images_remove(product_id: str, image_id: str, expected_version: int | None = None):
    version = (
        expected_version
        if expected_version is not None
        else call("GET", f"/products/{product_id}")["version"]
    )
    return call("DELETE", f"/products/{product_id}/images/{image_id}", params={"expected_version": version})


@command(rules, "show")
def rules_show(product: str | None = None):
    return call("GET", "/rules", params={"product_id": product} if product else {})


@command(rules, "history")
def rules_history(product: str | None = None):
    return call("GET", "/rules/history", params={"product_id": product} if product else {})


@command(rules, "apply")
def rules_apply(file: Path = typer.Option(..., "--file"), product: str | None = None):
    """Publish a policy version with an owner credential. Agents cannot change limits."""
    return call("PUT", "/rules", params={"product_id": product} if product else {}, data=document(file))


@command(requests, "list")
def requests_list(limit: int = 50, offset: int = 0):
    return call("GET", "/requests", params={"limit": limit, "offset": offset})


@command(requests, "create")
def requests_create(file: Path = typer.Option(..., "--file")):
    return call("POST", "/requests", data=document(file))


@command(requests, "show")
def requests_show(request_id: str):
    return call("GET", f"/requests/{request_id}")


@command(requests, "update")
def requests_update(request_id: str, file: Path = typer.Option(..., "--file")):
    current = call("GET", f"/requests/{request_id}")["request"]
    data = {
        k: current[k]
        for k in [
            "title",
            "description",
            "category",
            "quantity",
            "material",
            "stackable",
            "destination",
            "latest_delivery",
            "max_total",
            "max_deposit_percent",
            "preference",
        ]
    }
    data["expected_version"] = current["version"]
    data.update(document(file))
    return call("PUT", f"/requests/{request_id}", data=data)


for command_name, method in [("launch", "POST"), ("cancel", "POST"), ("matches", "GET"), ("compare", "GET")]:

    def register_request(name, verb):
        @command(requests, name)
        def operation(request_id: str):
            return call(verb, f"/requests/{request_id}/{name}")

    register_request(command_name, method)


@command(negotiations, "list")
def negotiations_list(status: str | None = typer.Option(None, "--state"), limit: int = 50, offset: int = 0):
    return call(
        "GET",
        "/negotiations",
        params={
            k: v for k, v in {"state": status, "limit": limit, "offset": offset}.items() if v is not None
        },
    )


for command_name, suffix, method in [
    ("show", "", "GET"),
    ("context", "/context", "GET"),
    ("retry", "/retry", "POST"),
]:

    def register_room(name, path, verb):
        @command(negotiations, name)
        def operation(room_id: str):
            return call(verb, f"/negotiations/{room_id}{path}")

    register_room(command_name, suffix, method)


@command(negotiations, "act")
def negotiations_act(room_id: str, file: Path = typer.Option(..., "--file")):
    """Submit a typed action containing claim_token and expected_version. No automatic claiming or approval."""
    return call("POST", f"/negotiations/{room_id}/actions", data=document(file))


@command(negotiations, "watch")
def negotiations_watch(
    room_id: str,
    timeout: int = typer.Option(60, min=1, max=3600),
    interval: float = typer.Option(2, min=1, max=30),
    jsonl: bool = False,
):
    """Watch persisted state changes for a bounded duration; output JSON Lines."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        item = call("GET", f"/negotiations/{room_id}")
        signature = json.dumps(item, sort_keys=True)
        if signature != last:
            typer.echo(json.dumps(item))
            last = signature
        if item["state"] in ("ordered", "declined", "cancelled", "expired"):
            return
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


@command(agent, "instructions")
def agent_instructions():
    return call("GET", "/agent/instructions")


@command(agent, "inbox")
def agent_inbox():
    return call("GET", "/agent/inbox")


@command(agent, "watch")
def agent_watch(
    timeout: int = typer.Option(60, min=1, max=3600),
    interval: float = typer.Option(2, min=1, max=30),
    jsonl: bool = False,
):
    """Watch your inbox for changed work, including human requests; output JSON Lines."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        item = call("GET", "/agent/inbox")
        signature = json.dumps(item, sort_keys=True)
        if signature != last:
            typer.echo(json.dumps(item))
            last = signature
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


@command(agent, "claim")
def agent_claim(room_id: str):
    return call("POST", f"/negotiations/{room_id}/claim")


@command(approvals, "status")
def approvals_status(room_id: str):
    return call("GET", f"/approvals/{room_id}")


@command(approvals, "open")
def approvals_open(room_id: str):
    result = call("GET", f"/approvals/{room_id}")
    webbrowser.open(result["approval_url"])
    return {
        "approval_url": result["approval_url"],
        "message": "A human must sign in and approve the exact offer in the browser.",
    }


@command(orders, "list")
def orders_list(limit: int = 50, offset: int = 0):
    return call("GET", "/orders", params={"limit": limit, "offset": offset})


@command(orders, "show")
def orders_show(order_id: str):
    return call("GET", f"/orders/{order_id}")


@command(app, "activity")
def activity(resource_id: str | None = None, limit: int = 50, offset: int = 0):
    return call("GET", "/activity", params={k: v for k, v in locals().items() if v is not None})


@command(app, "categories")
def categories():
    return call("GET", "/categories")


def main():
    app()


if __name__ == "__main__":
    main()
