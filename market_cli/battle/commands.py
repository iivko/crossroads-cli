"""Commands usable without any model provider dependency."""

import json
import time
from pathlib import Path
from typing import Literal

import typer

from .io import BattleError, load_config, read_private
from .runtime import Journal, run_external, run_lock

app = typer.Typer(help="Watch independent buyer/seller agents; optional bring-your-own-model Deep Agents.")


def execute(config, mode, resume, env_file):
    try:
        if env_file:
            read_private_env(env_file)
        settings = load_config(config)
        with run_lock(settings["run_dir"]):
            journal = Journal(settings["run_dir"], settings["room_id"], resume=resume)
            if mode == "external":
                result = run_external(settings, journal)
            else:
                from .deep import run_deepagents

                result = run_deepagents(settings, journal)
        typer.echo(json.dumps(result, indent=2))
    except BattleError as exc:
        typer.echo(json.dumps({"error": {"code": exc.code}}), err=True)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        typer.echo("Battle paused; use battle resume.", err=True)
        raise typer.Exit(130)
    except ImportError:
        typer.echo('Optional adapter unavailable. Install with: uv tool install ".[battle]"', err=True)
        raise typer.Exit(2)
    except Exception:
        # Provider errors can include headers or private prompts. Never render them.
        typer.echo(json.dumps({"error": {"code": "battle_failed_check_configuration"}}), err=True)
        raise typer.Exit(1)


def read_private_env(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise BattleError("env_file_must_be_mode_0600")
    from dotenv import load_dotenv

    load_dotenv(path, override=False)


@app.command()
def run(
    config: Path = typer.Option(..., help="Private JSON config (chmod 600)."),
    mode: Literal["external", "deepagents"] = "external",
    env_file: Path | None = typer.Option(None, help="Optional private model-provider environment file."),
):
    """Start the public monitor. External agents bring their own LLMs and tools."""
    execute(config, mode, False, env_file)


@app.command()
def resume(
    config: Path = typer.Option(...),
    mode: Literal["external", "deepagents"] = "external",
    env_file: Path | None = None,
):
    """Resume observation, or safely replay a pending optional-adapter action."""
    execute(config, mode, True, env_file)


@app.command()
def status(run_dir: Path = typer.Option(...)):
    """Print a sanitized local snapshot without exposing credentials or private rules."""
    try:
        state = read_private(run_dir / "state.json")
        typer.echo(json.dumps(Journal(run_dir, state["room_id"], resume=True).summary(), indent=2))
    except (BattleError, OSError, ValueError):
        typer.echo("No readable private battle state in this directory.", err=True)
        raise typer.Exit(1)


def euro(cents):
    return f"€{cents / 100:,.2f}"


@app.command()
def watch(
    run_dir: Path = typer.Option(...),
    role: Literal["buyer", "seller"] = typer.Option(...),
    timeout: int = typer.Option(3600, min=1, max=43200),
    jsonl: bool = False,
):
    """Display redacted offers in a terminal. It never reads private agent memory."""
    if not jsonl:
        typer.secho(
            f"CROSSROADS  |  {role.upper()} AGENT", fg="cyan" if role == "buyer" else "yellow", bold=True
        )
        typer.echo("Live offers · EUR · Human approval required\n")
    path = run_dir / f"{role}.jsonl"
    deadline, offset = time.monotonic() + timeout, 0
    try:
        while time.monotonic() < deadline:
            if path.exists():
                with path.open() as stream:
                    stream.seek(offset)
                    while line := stream.readline():
                        if not line.endswith("\n"):
                            break
                        offset = stream.tell()
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        if jsonl:
                            typer.echo(json.dumps(item))
                        else:
                            label = item["role"].upper()
                            typer.secho(
                                f"{item['at'][11:19]}  {label}: {item['event'].replace('_', ' ').upper()}",
                                fg="cyan" if item["role"] == "buyer" else "yellow",
                                bold=True,
                            )
                            typer.echo(item["message"])
                            if terms := item.get("terms"):
                                typer.echo(
                                    f"  {euro(terms['unit_price'])}/chair × {terms['quantity']}"
                                    f"  |  TOTAL {euro(terms['total'])}"
                                    f"  |  deposit {terms['deposit_percent']}%"
                                )
                                typer.echo(
                                    f"  Delivery {euro(terms['delivery_charge'])} on {terms['delivery_date']}"
                                )
                            if item.get("state"):
                                typer.echo(f"  State: {item['state']}")
                            typer.echo()
                        if (
                            item["event"] in ("finished", "timeout", "paused")
                            and offset == path.stat().st_size
                        ):
                            return
            time.sleep(0.3)
    except KeyboardInterrupt:
        return
