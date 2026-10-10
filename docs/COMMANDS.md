# Command reference

Syntax: `market [--profile NAME] [--base-url URL] [--workspace ID] GROUP COMMAND [arguments] [options]`.

Every leaf command accepts `--json` and `--idempotency-key KEY`. Use `--help` on a command for exact arguments. JSON files can be replaced by `--file -` to read stdin; this cannot be combined with another operation consuming stdin in the same invocation.

## API responses versus CLI output

HTTP JSON responses contain exactly one resource label and a numeric `status_code` matching the actual HTTP status. The label depends on the endpoint: a product uses `product`, a turn claim uses `claim`, and a negotiation action uses `action`. Errors use `error`. The [backend API reference](https://github.com/iivko/crossroads-backend/blob/main/docs/api.md) documents the full HTTP contract.

The CLI validates that envelope, then prints its resource payload. This keeps existing shell scripts compatible. These examples show the fields relevant to scripting:

| Operation | HTTP response | CLI output |
| --- | --- | --- |
| Product lookup | `{"product":{"id":"p1",...},"status_code":200}` | `{"id":"p1",...}` |
| Turn claim | `{"claim":{"claim_token":"...","expected_version":3,...},"status_code":200}` | `{"claim_token":"...","expected_version":3,...}` |
| Paginated products | `{"products":{"items":[...],"limit":50,"offset":0},"status_code":200}` | `{"items":[...],"limit":50,"offset":0}` |
| Workspace list | `{"workspaces":{"items":[...]},"status_code":200}` | `[...]` |
| Permission error | `{"error":{"code":"permission_denied","message":"...","details":null},"status_code":403}` | `{"error":{"code":"permission_denied","message":"...","details":null}}` on stderr; exit 4 |

Bare-array CLI outputs are preserved specifically for workspace lists, credential lists, categories, product history, image metadata, rule history and matching results. Paginated collections retain their pagination fields. Nested payload fields are never recursively unwrapped.

For example, continue reading IDs and claim tokens directly:

```sh
market products show PRODUCT_ID | jq -r '.id'
market agent claim ROOM_ID | jq -r '.claim_token'
market products list | jq '.items'
market products images list PRODUCT_ID | jq '.[0].id'
```

Image-content HTTP endpoints return image bytes on success. Their errors still use the JSON error envelope. `products images read` downloads those bytes and prints its local-file manifest as before; there is no base64 wrapper.

If the API sends a missing or wrong resource label, mismatched status, invalid JSON or an unexpected body type, the CLI returns `invalid_response` (exit 1). Its error details contain only `http_status`; it does not print the unexpected body, which could contain sensitive data. Ensure the CLI and API have been updated together before retrying. No transport failure grants permission to repeat a mutation with a new idempotency key.

## Identity and access

| Command | Purpose |
| --- | --- |
| `auth login --email EMAIL [--password-stdin]` | Authenticate a user; password prompt or environment input |
| `auth use-token [--token-stdin]` | Validate and store a preissued token; `MARKET_TOKEN` avoids storage |
| `auth whoami` | Show active user, workspace, credential kind and effective scopes |
| `auth logout` | Revoke current token and clear its local profile |
| `workspace list` | List accessible workspaces; agent tokens expose only their bound workspace |
| `workspace use ID` | Validate and select a workspace membership |
| `credentials list` | Admin lists own workspace's agent credentials without secrets |
| `credentials create --file FILE` | Admin issues role-compatible scopes; secret returned once |
| `credentials revoke ID` | Admin revokes an agent credential |

A credential document contains `name`, `scopes`, and optional `expires_days` (1–90, default 30).

## Catalogue and inventory

| Command | Purpose |
| --- | --- |
| `categories` | Read supported categories and fields |
| `products list [--q TEXT] [--own] [--material MATERIAL] [--destination CITY] [--limit N] [--offset N]` | Browse allowed catalogue |
| `products show ID` | Read one product and its gallery |
| `products create --file FILE` | Seller creates a product |
| `products update ID --file FILE` | Seller applies a partial edit merged with current fields |
| `products history ID` | Seller inspects immutable product revisions |
| `products stock set ID QUANTITY [--expected-version N]` | Seller updates current stock |

`examples/product.json` shows the full product schema. Product updates can contain only changed fields plus an optional `expected_version`. Nested `attributes` replaces the whole attributes object. Prices are integer cents, not floats. Draft products are private to their seller; `published:true` exposes the reviewed listing to authenticated catalogue readers.

## Images

| Command | Purpose |
| --- | --- |
| `products images upload PRODUCT FILE... [--caption TEXT]` | Upload one or more files; each independently validated |
| `products images list PRODUCT` | Read image metadata and processing states |
| `products images read PRODUCT [--all] [--image-id ID] [--output-dir DIR]` | Download a chosen image, first image, or all ready images |
| `products images open IMAGE [--output-dir DIR]` | Download and open a local file for a human |
| `products images cover PRODUCT IMAGE [--expected-version N]` | Select a ready cover image |
| `products images reorder PRODUCT --file FILE` | Set the exact ordered list of all ready image IDs |
| `products images remove PRODUCT IMAGE [--expected-version N]` | Remove from gallery, preserving historical evidence |

Reorder input: `{"image_ids":["first-id","second-id"],"expected_version":3}`. Omit `expected_version` to use the fetched current version.

Downloads cannot overwrite a different existing file or follow a destination symlink. Upload paths resolve inside `MARKET_MEDIA_ROOT`; a symlink escaping that directory is rejected. Image decoding and final authorization remain server-side. File extensions and captions are never accepted as proof that content is a valid image.

## Policies

| Command | Purpose |
| --- | --- |
| `rules show [--product ID]` | Own stored/effective rules and version numbers |
| `rules history [--product ID]` | Own policy history |
| `rules apply --file FILE [--product ID]` | Admin publishes the next policy version |

Policy input: `{"expected_version":1,"rules":{...}}`. A workspace without a stored policy starts at version 0. Buyer policies support `max_total` and `max_deposit_percent`. Seller policies support `min_unit_price`, `max_discount_percent`, `min_deposit_percent`, `discount_min_quantity`. Product policies are seller-only and cannot relax workspace limits. No agent token has `policies:write`.

## Buyer requests

| Command | Purpose |
| --- | --- |
| `requests list [--limit N] [--offset N]` | Own purchase requests |
| `requests create --file FILE` | Create a reviewed structured draft |
| `requests show ID` | Request, matching reasons and supplier rooms |
| `requests update ID --file FILE` | Edit draft or unmatched request; merges partial input |
| `requests launch ID` | Match published products and open rooms |
| `requests cancel ID` | Cancel an unconfirmed request and clear approvals |
| `requests matches ID` | Supplier eligibility and concrete exclusion reasons |
| `requests compare ID` | Compare current offers; best fit and cheapest are separately identified |

Required fields: title, quantity, material, stackable, destination, latest_delivery, max_total, max_deposit_percent. Optional description, category (`chairs`) and preference (`lowest_total`, `lower_deposit`, `earliest_delivery`). The description stays private to the buyer. This release expects structured input; your external agent can extract natural language before preparing it.

The sample request file uses an illustrative date. Replace it before use, or generate a current example with `uv run python -m market.manage example-request` in the backend checkout.

## Negotiation and agent protocol

| Command | Purpose |
| --- | --- |
| `negotiations list [--state STATE] [--limit N] [--offset N]` | Participant's rooms |
| `negotiations show ROOM` | Offers, approvals, activity and current state |
| `negotiations context ROOM` | Permitted next actions, private own-side limits and public facts |
| `agent instructions` | Schemas, role permissions and operating guidance |
| `agent inbox` | Discover rooms and whether this role can act |
| `agent claim ROOM` | Acquire a short turn lease |
| `negotiations act ROOM --file FILE` | Submit a structured action |
| `negotiations watch ROOM [--timeout SECONDS] [--interval SECONDS] --jsonl` | Observe persisted state changes; default timeout 60 seconds |
| `negotiations retry ROOM` | Buyer user starts a new bounded attempt after decline, expiry or escalation |

An action document includes `type`, `expected_version`, `claim_token`, and action-specific fields. Propose/counter require `terms`; accept requires `offer_id`; clarifications require a topic (`specifications`, `delivery`, `images`, `quantity`). Decline and human review take no terms/topic/offer ID. Extra fields and arbitrary model-generated reasoning are rejected.

Lease expiry defaults to 180 seconds. There is no automatic lease renewal. If inference outlasts the lease, fetch context and reclaim the turn; the backend rejects the old claim. Never accept your own offer. The first proposal and invalidated-offer revisions belong to the seller.

## Approval, orders and audit

| Command | Purpose |
| --- | --- |
| `approvals status ROOM` | Inspect exact terms and approval status |
| `approvals open ROOM` | Open the human review URL; does not approve |
| `orders list [--limit N] [--offset N]` | Own confirmed orders |
| `orders show ID` | Approved terms, both approvals and product evidence |
| `activity [--resource-id ID] [--limit N] [--offset N]` | Workspace audit history |

Agent `accept` means the terms satisfy its side. Human approval is a separate browser-only operation. There is no CLI override or `--yes` switch for approving a deal.
