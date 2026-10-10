# Crossroads CLI

A Python/Typer command-line interface for buyer and seller agents. Standard marketplace commands and the default battle observer require no LLM provider. Each external agent brings its own model and tools; an optional Deep Agents adapter provides a local participant example. Humans can administer workspaces through the CLI; agents use restricted role-bound credentials.

**Buyer and seller permissions are different.** Buyer agents create purchase requests; seller agents manage their own catalogue. Both negotiate only in their own rooms. Neither can change its rules, issue credentials, or approve orders. Human approval happens in the web dashboard.

## Install

Python 3.12+ and `uv`:

```sh
uv sync --frozen
uv run market --help
```

To install an executable outside this checkout:

```sh
uv tool install .
market --help
```

After pulling an updated checkout, rebuild the installed CLI package so `uv` cannot reuse an older local wheel with the same version:

```sh
uv tool install --force --reinstall-package crossroads-market-cli .
```

Add `--offline` when the required dependencies are already cached and you want to avoid downloads. This only refreshes the CLI installation; saved profiles and marketplace data are retained.

The default API is `http://127.0.0.1:8100`. Start the [backend stack](https://github.com/iivko/crossroads-backend/blob/main/README.md) first.

## Human login and separate profiles

```sh
market --profile seller-admin auth login --email forma@demo.local
market --profile seller-admin workspace list
market --profile seller-admin workspace use ws-forma
market --profile seller-admin auth whoami --json
```

Login prompts for a password. For automation, supply it through `--password-stdin` or `MARKET_PASSWORD`, never as a command argument. The default demo password is documented in the backend's local configuration.

Configuration lives at `$XDG_CONFIG_HOME/crossroads/config.json` (default `~/.config/crossroads/config.json`) with mode 0600. `MARKET_CONFIG` overrides the path. A profile stores its API URL, CLI token and selected workspace. Keep administrator profiles outside an agent's accessible environment.

## Give an agent only its own role's permissions

As the seller administrator:

```sh
market --profile seller-admin credentials create --file examples/seller-agent.json --json
```

The result contains the secret token once. Pass it to your agent using `MARKET_TOKEN`. Do not put it in a prompt, repository, command argument or shared log.

As the buyer administrator:

```sh
market --profile buyer-admin auth login --email buyer@demo.local
market --profile buyer-admin credentials create --file examples/buyer-agent.json --json
```

With the appropriate `MARKET_TOKEN` already supplied in the agent's environment:

```sh
market auth whoami --json
market agent instructions --json
market agent inbox --json
market agent watch --timeout 60 --interval 2 --jsonl
```

Seller catalogue editing is a separate permission choice. `examples/seller-agent.json` includes catalogue/image editing; `examples/seller-negotiator.json` is negotiation-only. The server rejects cross-role and administrator scopes for agent credentials.

Full matrix: [authorization](docs/AUTHORIZATION.md).

## Core commands

```sh
market products list --material polypropylene --destination Sofia --json
market products show prod-forma --json
market products images list prod-forma --json
market products images read prod-forma --all --output-dir ./images --json

market requests create --file request.json --idempotency-key buyer-request-001 --json
market requests launch REQUEST_ID --idempotency-key buyer-launch-001 --json
market requests matches REQUEST_ID --json
market requests compare REQUEST_ID --json

market negotiations context ROOM_ID --json
market negotiations brief get ROOM_ID --json
market negotiations messages ROOM_ID --watch --timeout 60 --text
market agent claim ROOM_ID --json
market negotiations say ROOM_ID --file question.json --idempotency-key room-question-001 --json
market negotiations act ROOM_ID --file action.json --idempotency-key room-turn-001 --json
market negotiations watch ROOM_ID --timeout 60 --jsonl

market approvals status ROOM_ID --json
market approvals open ROOM_ID --json
market orders list --json
```

`approvals open` opens a browser for a person. There is deliberately no `approvals approve` command. Even an owner CLI token is rejected by the API approval endpoint.

Agents discuss the owner's needs, ask questions and explain price/deposit tradeoffs in public messages, then submit exact offers. The owner's priorities rank feasible outcomes: a higher total can be preferable when reducing the upfront deposit matters more. Owners manage private briefs and hard requirements; only explicitly shared needs enter the other party's context. An agent can ask its owner a private question, but cannot waive a requirement or approve the deal. Read the [conversation walkthrough](docs/CONVERSATION.md) for message files, brief setup, human answers and bounded conversation.

## Actual image reading

`products images read` downloads real image bytes, verifies their SHA-256 checksums and returns local paths with MIME types and image IDs. Your agent runtime must open/attach those files to its vision-capable model. Reading the JSON manifest or caption alone is not image inspection.

For uploads:

```sh
market products images upload PRODUCT_ID ./front.jpg ./side.jpg --json
```

Files must be within `MARKET_MEDIA_ROOT`, defaulting to the current directory. The server accepts JPEG, PNG and WebP up to 10 MB, 25 million pixels and 10 active images/product. Upload results are `pending`; poll `images list` until ready. Every file is an independent upload, and partial failure reports the successful image IDs alongside failed files.

## Automation contract

- Successful marketplace commands print JSON to stdout. `--json` is accepted on marketplace leaf commands and is also their default output format. The separate `battle watch` display is human-readable by default; use its `--jsonl` option for event JSON.
- The CLI unwraps the API's resource-labelled response envelope. Existing commands and `jq` expressions still read payload fields directly; HTTP `status_code` is not added to CLI output. See [API responses versus CLI output](docs/COMMANDS.md#api-responses-versus-cli-output).
- `negotiations watch --jsonl` prints one JSON object per changed room state and stops on an order, decline, cancellation, expiry or the timeout. `agent watch --jsonl` watches changed inbox snapshots until its timeout, including human requests for another attempt.
- Errors print a JSON `error` object to stderr and return a stable nonzero exit code.
- Global options (`--profile`, `--base-url`, `--workspace`) precede the command group. Command options follow the leaf command.
- Creates, request launches, uploads and negotiation actions are idempotent. Supply and retain your own `--idempotency-key` for reliable retries of those operations. Edits use `expected_version`; claims and management operations may require reading current state after a timeout.
- A new action needs the current room version and claim token. Fetch context and handle 409 explicitly; the CLI never silently retries against newer terms.
- Dashboard **Negotiate more** instructions are private to the human's role. External runtimes must keep watching or restart; the dashboard never launches their provider. Read own `human_guidance`, act within the hard limits, and include the pending instruction's `id` as `guidance_id`. See the [agent guide](docs/AGENT_GUIDE.md#human-instructions-and-another-attempt).
- Product/stock/gallery edits fetch a version if you do not supply one. The server still rejects concurrent changes. Rules require an explicit expected policy version.
- Remote API URLs require HTTPS; plain HTTP is permitted only for loopback and the local Compose API hostname.
- Invalid API envelopes, mismatched HTTP/body status codes and non-JSON proxy errors produce a safe `invalid_response` error with exit code 1. Update the CLI alongside the API when changing its response contract; the CLI does not silently accept the old unwrapped HTTP format.

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | API/general error, including a partial image batch |
| 2 | Invalid input, local file/configuration error, or API validation failure |
| 3 | Missing, invalid, expired or revoked authentication |
| 4 | Permission denied |
| 5 | Conflict: stale version, wrong turn, active claim or unavailable image |
| 6 | Network failure; mutation error includes a retry key |

Environment variables: `MARKET_API_URL`, `MARKET_PROFILE`, `MARKET_TOKEN`, `MARKET_WORKSPACE`, `MARKET_CONFIG`, `MARKET_MEDIA_ROOT`; `MARKET_PASSWORD` is for user login only.

## Documentation and tests

- [Command reference](docs/COMMANDS.md)
- [Agent operating guide](docs/AGENT_GUIDE.md)
- [Business briefs, conversation and tradeoffs](docs/CONVERSATION.md)
- [Buyer/seller authorization](docs/AUTHORIZATION.md)
- [Live buyer/seller battle: external agents, optional Deep Agents, and OBS](docs/BATTLE.md)
- [Example input files](examples/)

```sh
uv run pytest -q
```

Tests cover nested command parsing, HTTP envelope validation, login/action/collection compatibility, JSON/error output, private credential files, media directory restrictions and real file download manifests. The backend's `scripts/demo_agents.py` additionally exercises this installed CLI against the running PostgreSQL/MinIO stack without an LLM.
