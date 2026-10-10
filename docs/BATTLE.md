# Live buyer versus seller negotiation

Crossroads is a marketplace protocol and CLI. **Each participant brings its own
agent, model, and tools.** The buyer and seller can use different providers,
frameworks, or machines. Their restricted credentials and the server's rules
determine what they may do.

`market battle run` starts a local observer by default. It does not call a model,
choose prices, claim a turn, or start the independent agents. It follows real
API activity and writes safe terminal feeds. The optional `--mode deepagents`
adapter provides a runnable two-participant example when both parties explicitly
configure models; it is not a requirement for using the marketplace.

## Pricing: private floor, public asking price, private budget

The chair demonstration uses these terms:

| Field | Demo value | Visibility |
| --- | --- | --- |
| Quantity and available stock | 8 chairs | Public product/request facts |
| Public `product.unit_price` | 14,000 cents (€140/chair) | Both parties |
| Seller `min_unit_price` policy | 10,000 cents (€100/chair) | Seller only |
| Delivery charge | 24,000 cents (€240/order) | Both parties |
| Buyer `max_total` | 120,000 cents (€1,200 including delivery) | Buyer only |
| Seller minimum deposit | 20% | Seller policy |
| Buyer maximum deposit | 30% | Buyer policy |

The human seller's entered €100 minimum remains their private floor. The seller
agent tries to earn more. A counteroffer never changes the floor. The public
asking price is a separate catalogue field, so buyers see the opening commercial
position without receiving the seller's private rule.

The seller's minimum total is `8 × €100 + €240 = €1,040`. The buyer can afford at
most €120/chair when delivery is €240. The public opening ask totals €1,360; the
agents must negotiate to find an acceptable overlap. The result is not scripted
or guaranteed. The buyer can try a price below the seller's floor, but the seller
cannot submit or accept terms that violate its own rules. The API checks each
role's actions and both sides' effective rules before an agreement.

For this demo, `max_discount_percent` is 100 and `min_unit_price` is €100, so the
explicit floor controls the lowest acceptable price. In other setups, a stricter
maximum-discount rule can raise the effective floor. Product rules may tighten
workspace rules; they cannot loosen them. See [policy commands](COMMANDS.md#policies).

## 1. Prepare the marketplace and product

Start the backend, database, MinIO, image worker and frontend. Keep human owner
credentials separate from agent credentials. Give each participant only its own
workspace ID and agent token.

The backend's
[`scripts/battle_demo.py`](https://github.com/iivko/crossroads-backend/blob/main/scripts/battle_demo.py)
is an explicit chair-demo bootstrap. After an operator has backed up and reset
the intended local demo database/storage, run from the backend repository:

```bash
uv run python scripts/battle_demo.py --prepare \
  --image /absolute/path/to/stol.jpg
```

The backend environment must enable `DEMO_ENABLED` and supply a demo password of
at least 12 characters. The preparation command refuses unrelated existing
data. It does **not** reset or delete a database/storage itself, and it does not
start models. It creates the buyer/seller accounts, the chair listing, the real
image, policies, a matching purchase request, a room, and two restricted agent
credentials. It waits for the image worker, verifies the downloaded image, and
can resume its own interrupted preparation.

Its default private configuration is `backend/work/battle/config.json`; its run
directory is `backend/work/battle/run`. The configuration contains **both** agent
tokens and belongs only to the trusted local operator. Never hand the combined
file to either external agent, add it to Git, or show it in an OBS terminal.

For another marketplace setup, create an equivalent private configuration with
the existing room and credentials. The placeholders below are explanatory;
replace them locally and protect the file with `chmod 600`:

```json
{
  "api_url": "http://127.0.0.1:8100",
  "room_id": "ROOM_ID",
  "run_dir": "/absolute/private/path/to/battle/run",
  "timeout_seconds": 900,
  "pause_seconds": 3,
  "max_model_calls": 32,
  "buyer": {
    "workspace_id": "BUYER_WORKSPACE_ID",
    "token": "BUYER_AGENT_TOKEN"
  },
  "seller": {
    "workspace_id": "SELLER_WORKSPACE_ID",
    "token": "SELLER_AGENT_TOKEN"
  }
}
```

The run directory must have mode 0700, and its private state/configuration files
must have mode 0600. The CLI creates new run directories and state with these
permissions and rejects an existing shared directory or readable private file.

## 2. Start the default provider-neutral observer

Set paths for your checkout, then start the observer in its own operator shell:

```bash
BATTLE_CONFIG=/absolute/path/to/backend/work/battle/config.json
BATTLE_RUN_DIR=/absolute/path/to/backend/work/battle/run

market battle run --config "$BATTLE_CONFIG"
```

`--mode external` is the default. It validates that the two distinct tokens are
bound to the correct buyer/seller workspaces and are restricted agent
credentials. It polls the room and waits for independent agents to act. No LLM
dependency or provider API key is needed by this observer.

Open two terminals, using the same absolute run-directory path in each:

```bash
# Buyer display terminal
market battle watch --run-dir "$BATTLE_RUN_DIR" --role buyer
```

```bash
# Seller display terminal
market battle watch --run-dir "$BATTLE_RUN_DIR" --role seller
```

The displays show real submitted offers, who made them, delivery-inclusive
totals, deposits, dates and room status. Both feeds include the public exchange
so each side's counteroffer remains understandable. They do not display private
prompts, model reasoning, budgets, policy floors, credentials, claim tokens or
raw provider exceptions. The messages are fixed descriptions of actual API
events rather than invented agent dialogue. `--jsonl` emits those same safe
events as JSON Lines.

Watchers wait for their feed to appear. Their default timeout is one hour; use
`--timeout SECONDS` to change it. They exit after a finished, paused or timeout
event. Restart a watcher to replay the public feed from the beginning.

## 3. Start each independent participant

Each participant receives **only its own** restricted token through its runtime
environment, its workspace ID, the API URL and the room ID. Its runtime must
provide a vision-capable model and a way to attach the image files returned by
the CLI. Reading image captions or filenames does not count as inspecting the
image.

The participant loop is:

1. Read `market agent instructions --json` and its own
   `market negotiations context ROOM_ID --json`.
2. Read the product and download its ready gallery with
   `market products images read PRODUCT_ID --all --output-dir PRIVATE_DIR --json`.
   Attach the verified image bytes/files to its own model.
3. Inspect the current room state, version, allowed actions and own private
   limits. Wait if it is the other participant's turn.
4. Choose a structured proposal, counteroffer, acceptance, decline or supported
   clarification. The seller maximizes proceeds above its floor; the buyer
   minimizes the delivery-inclusive total within its budget.
5. Refresh context after model inference, acquire its turn with
   `market agent claim ROOM_ID --json`, and submit the exact action through
   `market negotiations act ROOM_ID --file PRIVATE_ACTION_FILE
   --idempotency-key UNIQUE_KEY --json`.
6. Retain the action and idempotency key until its outcome is known. Reconcile
   conflicts against the API instead of silently changing a stale action.

The backend's
[`battle_participant.py`](https://github.com/iivko/crossroads-backend/blob/main/scripts/battle_participant.py)
is an optional local Codex CLI participant example. It is an external client of
the marketplace. Replace it with your own agent runtime; the API and CLI do not
require Codex, Deep Agents, a shared provider, or a marketplace-hosted LLM.

Use the example's `--help` for its operator arguments. Its private runtime files
and model output belong outside the captured terminals. The public viewers above
are the screens to capture. Keep the two external participants in separate
processes/environments so neither receives the other's credentials or context.

The example uses separate profiles and work directories under one OS user for
trusted local participants. These directories do not enforce filesystem
isolation against a hostile agent. Use separate machines or containers for
independent counterparties; the [backend guide](https://github.com/iivko/crossroads-backend/blob/main/docs/BATTLE.md)
explains local runtime execution options.

Full protocol details: [agent operating guide](AGENT_GUIDE.md) and
[command reference](COMMANDS.md#negotiation-and-agent-protocol).

## 4. Resume, inspect and finish

```bash
market battle status --run-dir "$BATTLE_RUN_DIR"
market battle resume --config "$BATTLE_CONFIG"
```

`status` returns a sanitized local snapshot. `resume` reconciles the current API
room with the existing journal, preserving already observed offers instead of
creating a new room. Use it after interruption or an observer timeout. Only one
controller can hold a run directory's lock at a time.

An external observer timeout stops observation, **not** independently running
agents. Each participant must enforce its own wall-clock/retry bounds in addition
to the backend's turn, lease and counteroffer limits. Stop or pause participant
processes separately when that is the operator's intent.

The API enforces the bounded negotiation, including three counteroffers per
party and expiring claims. Agreement transitions to `awaiting_approval`. Neither
agent nor the observer can approve an order: both designated humans must review
the exact terms in the dashboard. Stock is committed only through the existing
validated order/approval flow. A decline, expiry or human-review request is a
valid outcome; the demo does not force an agreement.

### Follow dashboard requests to negotiate more

Either human can choose **Negotiate more**, enter a private tactic or objective,
and queue another bounded attempt for its own agent. This resets approvals and
claims and gives that role the next turn. The instruction is available only in
that participant's context. Public battle feeds show a generic human-request
event and the resulting offers, never the instruction text.

By default the observer and optional Deep Agents adapter still finish at the
first agreement, decline, expiry or human-review request. Opt in to waiting:

```bash
market battle run --config "$BATTLE_CONFIG" --wait-for-guidance

# For a previously started battle:
market battle resume --config "$BATTLE_CONFIG" --wait-for-guidance

# Optional participant controller, configured with your own models:
market battle resume --config "$BATTLE_CONFIG" --mode deepagents --wait-for-guidance
```

The flag waits through those states until a human starts a new run or the existing
wall-clock limit expires. Ordered and cancelled rooms always stop the controller.
It does not reset the model-call budget, and waiting itself uses no model calls.
An external observer cannot restart an external LLM process: keep each participant
running with `market agent watch --timeout 60 --interval 2 --jsonl`, or restart
your runtime against the existing room. The dashboard queues work and never
starts or configures the participant's provider by itself.

Participating runtimes must read their own `human_guidance` and acknowledge the
latest pending ID as `guidance_id` in the next valid action. The optional adapter
does this automatically after checking fresh version, run and guidance. Its
pending-action journal retains the ID when replaying an uncertain submission.
Human tactics cannot override private floors, budget ceilings or other server
limits. See the [agent protocol](AGENT_GUIDE.md#human-instructions-and-another-attempt).

## Optional: run the Deep Agents example

Install the optional participant adapter from the CLI repository:

```bash
uv tool install --force --reinstall-package crossroads-market-cli '.[battle]'
```

For a checkout-local command, use `uv run --extra battle market ...` instead.
The extra installs Deep Agents, LangGraph, and the OpenAI/Ollama LangChain
integrations. Other providers need their LangChain integration installed in the
same environment. Select models that support image input, tool calling and
structured output.

Add a participant-selected `model` to **each** role in the private configuration:

```json
{
  "buyer": {
    "workspace_id": "BUYER_WORKSPACE_ID",
    "token": "BUYER_AGENT_TOKEN",
    "model": "openai:YOUR_VISION_MODEL",
    "model_kwargs": {}
  },
  "seller": {
    "workspace_id": "SELLER_WORKSPACE_ID",
    "token": "SELLER_AGENT_TOKEN",
    "model": "ollama:YOUR_VISION_MODEL",
    "model_kwargs": {"base_url": "http://127.0.0.1:11434"}
  }
}
```

This fragment shows the two role entries; retain `api_url`, `room_id`, `run_dir`
and the bounds from the complete configuration. Replace model placeholders with
models actually available to each participant. Supply provider credentials in
the operator environment, or a private 0600 dotenv file passed with `--env-file`.
Never include provider keys in a command argument, captured terminal or public
event feed.

```bash
market battle run --config "$BATTLE_CONFIG" --mode deepagents \
  --env-file /absolute/private/path/to/providers.env

# After interruption, use the same mode and configuration:
market battle resume --config "$BATTLE_CONFIG" --mode deepagents \
  --env-file /absolute/private/path/to/providers.env
```

The optional local controller creates one Deep Agent per role, with separate
private context, marketplace adapter and persisted virtual files. Both inspect
the real product images. LangGraph refreshes API state, runs the correct
participant, validates its decision, obtains a claim and submits through the
CLI. Pending actions retain their exact payload and idempotency key for recovery.
The API remains authoritative.

The models have no arbitrary shell, delegation, policy-editing or approval tool.
This adapter is a trusted local demonstration process with separated role
contexts; it is not an operating-system sandbox for mutually untrusted tenants.
Independent deployments should keep each participant's secrets and execution
environment under that participant's control.

`timeout_seconds` defaults to 900, `max_model_calls` to 32 and `pause_seconds` to
3. The model-call budget persists across resumes; the wall-clock timeout applies
to each invocation. Both budgets and API constraints bound the demonstration.
No mode silently picks or changes the participants' models.

## OBS and troubleshooting

The backend's [OBS live-view guide](https://github.com/iivko/crossroads-backend/blob/main/docs/OBS_BATTLE.md)
explains the two terminal captures, authenticated OBS MCP setup, live projector
and screenshot verification. OBS does not need participant tokens or model keys.

| Result or error | What to do |
| --- | --- |
| Observer waiting with no offers | Start the independent seller participant; default external mode never launches a model. |
| `battle_exists_use_resume` | Reuse the journal with `battle resume`; do not delete it to retry an uncertain action. |
| `battle_controller_already_running` | Keep one observer/controller per run directory. |
| `private_file_must_be_mode_0600` | Protect the config/state file and remove any symlink. |
| `run_directory_must_be_private` | Apply `chmod 700` to the intended private run directory. |
| `restricted_agent_credentials_required` | Supply distinct correctly scoped buyer/seller agent tokens, not human owner tokens. |
| `participant_model_required_for_deepagents` | Configure a provider-qualified model for each participant, or use default external observation. |
| `turn_claimed_resume_after_claim_expires` | Check whether another participant owns the live turn; wait for its claim to expire before resuming the optional adapter. |
| Provider/configuration error | Inspect configuration privately; the public command deliberately omits raw provider errors that may contain secrets. |

The full permission matrix remains in [authorization](AUTHORIZATION.md). Buyer
credentials cannot edit seller products; seller credentials cannot read buyer
private limits or administer buyer requests. Both are limited to their own rooms,
and neither can change policies or grant itself additional permissions.
