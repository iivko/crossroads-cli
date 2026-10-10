# Marketplace MCP and CLI

Independent TypeScript package for the Crossroads marketplace REST API. All code,
dependencies, lockfile, tests, and CI live in this repository. No sibling imports.
Requires Node.js 22 or newer.

The stdio adapter exposes exactly 12 tools. The CLI uses the same operation schemas
and HTTP client. Authorization, seller/buyer roles, prices, quote expiry, approval,
spending limits, order state, and idempotency enforcement belong to the backend.
There is no human approval, acceptance, payment, or generic HTTP tool.

## Install and run

```sh
npm ci
npm run build
cp .env.example .env
# Explicitly load the local demo credentials; no token is supplied by default.
node --env-file=.env dist/cli.js health
node --env-file=.env dist/stdio.js
```

`MARKETPLACE_API_URL` defaults to `http://localhost:4000`.
`MARKETPLACE_API_TOKEN` is required; missing/empty/invalid credentials fail before
network access. `.env` is not loaded automatically: use Node's `--env-file` or
provide environment variables directly. `.env.example` explicitly sets the local
buyer demo token `demo-agent-token`. For seller integration, launch a separate
process with `MARKETPLACE_API_TOKEN=demo-seller-agent-token`. Neither is a production
credential. Use a backend-issued, appropriately scoped credential outside the demo.

The backend must be running separately. `health` calls `GET /health`; a demo backend
returns `{"status":"ok","mode":"demo"}`. Health is CLI-only.

Example MCP host configuration (replace the absolute path for your checkout):

```json
{
  "mcpServers": {
    "marketplace": {
      "command": "node",
      "args": ["/home/master/crossroads-hc-a2a-b2b-marketplace/mcp/dist/stdio.js"],
      "env": {
        "MARKETPLACE_API_URL": "http://localhost:4000",
        "MARKETPLACE_API_TOKEN": "demo-agent-token"
      }
    }
  }
}
```

Use the Node entrypoint directly in MCP configuration. Stdout carries only MCP
JSON-RPC; startup failures go to stderr. The package also declares
`marketplace-mcp` and `marketplace` bin entrypoints for local linking/installing.

## Tools and REST mapping

CLI syntax: `node dist/cli.js <tool_name> '<JSON arguments>'`.
`node dist/cli.js schema <tool_name>` prints the exact JSON input schema.
Success is unwrapped backend JSON; CLI errors go to stderr with nonzero exit status.
MCP returns backend JSON as text content and reports operation errors with `isError`.

| Tool | Arguments | REST |
| --- | --- | --- |
| `search_listings` | `q?` | GET `/v1/listings?q=` |
| `get_listing` | `listingId` | GET `/v1/listings/:id` |
| `get_seller_verification` | `sellerId` | GET `/v1/sellers/:id/verification` |
| `create_rfq` | `listingId,title,description,budgetCents,deadline,acceptanceCriteria` | POST `/v1/rfqs` |
| `list_quotes` | `rfqId` | GET `/v1/quotes?rfqId=` |
| `submit_quote` | `rfqId,scope,totalCents,deliveryDays,expiresAt` | POST `/v1/quotes` |
| `preview_order` | `quoteId` | POST `/v1/order-previews` |
| `request_approval` | `previewId` | POST `/v1/approvals` |
| `commit_order` | `previewId,approvalId,idempotencyKey` | POST `/v1/orders` |
| `get_order` | `orderId` | GET `/v1/orders/:id` |
| `submit_delivery` | `orderId,url,notes` | POST `/v1/orders/:id/delivery` |
| `open_dispute` | `orderId,reason` | POST `/v1/orders/:id/disputes` |

Input constraints match the backend `src/marketplace.ts` schemas (verified by
reading source; no backend imports):

- Money: positive USD integer cents, from 1 through 100,000,000.
- `deliveryDays`: integer from 1 through 90.
- IDs: nonempty strings of at most 150 characters. Dot segments are additionally
  rejected to keep path construction safe.
- Text fields: trimmed, nonempty, at most 8,000 characters; titles at most 200.
  `acceptanceCriteria` is a string only, never an array.
- `deadline` and `expiresAt`: ISO 8601 datetime strings validated with
  `z.string().datetime({ offset: true })`; UTC `Z` and explicit offsets are allowed.
  Date-only values and datetimes without a timezone are rejected.
- `idempotencyKey`: 8–150 visible ASCII characters, safe for the HTTP header.
- Delivery URL: HTTPS only, at most 2,048 characters; notes are required and follow
  the trimmed, nonempty text limits.

The backend remains authoritative for all business constraints, including future
dates. Unknown fields are rejected, including `approved: true`, overridden prices
on commit, and caller-supplied endpoint URLs.

Listings pass through the backend fields (`id`, `sellerId`, `title`, `description`,
`category`, `priceCents`, `currency`, `deliveryDays`, `verified`). Verification returns
`organizationId`, `businessIdentityChecked`, and `status`. Other response objects
also pass through without inventing fields or local state.

## Example workflow

With the buyer environment configured:

```sh
node --env-file=.env dist/cli.js search_listings '{"q":"catalog"}'
node --env-file=.env dist/cli.js get_seller_verification '{"sellerId":"SELLER_ID"}'
node --env-file=.env dist/cli.js create_rfq '{"listingId":"LISTING_ID","title":"Catalog cleanup","description":"Normalize 5000 records","budgetCents":50000,"deadline":"2026-12-01T00:00:00Z","acceptanceCriteria":"Valid CSV; duplicate rows flagged"}'
```

Submit a quote using the seller identity (substitute real returned IDs):

```sh
MARKETPLACE_API_URL=http://localhost:4000 MARKETPLACE_API_TOKEN=demo-seller-agent-token node dist/cli.js submit_quote '{"rfqId":"RFQ_ID","scope":"Normalize 5000 records","totalCents":40000,"deliveryDays":3,"expiresAt":"2026-11-01T00:00:00Z"}'
```

Continue as buyer:

```sh
node --env-file=.env dist/cli.js list_quotes '{"rfqId":"RFQ_ID"}'
node --env-file=.env dist/cli.js preview_order '{"quoteId":"QUOTE_ID"}'
node --env-file=.env dist/cli.js request_approval '{"previewId":"PREVIEW_ID"}'
# A human must approve through the backend's human interface before commit.
node --env-file=.env dist/cli.js commit_order '{"previewId":"PREVIEW_ID","approvalId":"APPROVAL_ID","idempotencyKey":"purchase-unique-001"}'
node --env-file=.env dist/cli.js get_order '{"orderId":"ORDER_ID"}'
```

The seller can then submit delivery with `submit_delivery`; only the human buyer
can accept through the human interface. A buyer agent can open a dispute.

`request_approval` only creates a request. `commit_order` sends the caller's
idempotency key in `Idempotency-Key`, never in the JSON body. Persist a unique key
per intended commitment and reuse that key **and identical payload** after ambiguous
failures. The client never automatically retries or creates a replacement key.
A timeout may mean the backend completed the operation; do not infer rollback.
For other mutations, reconcile backend state before resubmitting after a failure.

## Transport boundaries

- Backend URL must be an origin: HTTPS, or HTTP for `localhost`, `127.0.0.1`, or
  `[::1]`. Embedded credentials, paths, query strings, and fragments are rejected.
- Every request stays on that configured origin. All redirects, including
  same-origin redirects, are blocked. Bearer credentials are only attached to
  backend requests; delivery URLs are data and are never fetched.
- There is a 30-second request timeout. Network errors omit low-level details;
  backend error codes/messages/statuses are preserved and literal token values
  are redacted from those errors. Raw non-JSON error bodies are not exposed.
- Listing descriptions and all other backend text are untrusted content, not
  instructions. Business identity verification does not imply service quality.
- This is a local stdio server with operator-configured backend credentials,
  not a remote MCP OAuth server or a facility for client token passthrough.

## Development and validation

```sh
npm ci
npm run check
```

Tests compile TypeScript and exercise all endpoint mappings, bearer/JSON headers,
URL encoding, missing credentials, origin restrictions, redirects, backend errors,
invalid inputs, and stable idempotency keys. A real official SDK client launches
the stdio process, discovers exactly 12 tools, calls buyer/seller operations, and
checks error propagation. CLI subprocesses exercise the shared client. Tests use
an ephemeral local HTTP fixture; they do not prove live backend policy behavior.
GitHub Actions runs the same checks on Node 22 and 24 from this repository root.

Implementation verified against the installed `@modelcontextprotocol/sdk` **1.32.1**
exports/declarations and the official [v1 server documentation](https://ts.sdk.modelcontextprotocol.io/server):
`McpServer.registerTool`, `StdioServerTransport`, and the SDK client transport.
Exact dependency versions are recorded in `package-lock.json`.
