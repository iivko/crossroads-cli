# Buyer and seller authorization

The API enforces role, workspace ownership and token scope together. Local CLI checks are not the security boundary.

| Capability | Buyer agent | Seller agent |
| --- | --- | --- |
| Published catalogue/image reading | Allowed | Allowed |
| Product creation, stock and gallery editing | Denied | Own products with explicit write scopes |
| Purchase request creation/edit/launch/cancel | Own requests only | Denied |
| Private buyer requirements/budget | Own workspace only | Denied |
| Private supplier rules | Denied | Own workspace/product only |
| Offers and negotiation context | Participating buyer only | Participating seller only |
| Initial proposal / revised invalid offer | Denied | Allowed on seller turn |
| Counter / accept / decline | Own turn, within buyer rules | Own turn, within seller rules |
| Rule changes | Denied | Denied |
| Credential administration | Denied | Denied |
| Human approval | Denied | Denied |
| Confirmed order access | Participating buyer only | Participating seller only |

An owner CLI session may manage policies and agent credentials inside its own workspace. It still cannot approve a purchase. Human approval requires an authorized browser session and session-specific CSRF token. Keep that browser session and the owner's password away from agent runtimes.

Tokens are workspace-bound. Changing `--workspace` does not grant membership or permit an agent to switch businesses. A seller owner cannot issue buyer scopes, and a buyer owner cannot issue seller scopes. Tokens can be revoked using `credentials revoke`; a revoked token loses access on subsequent requests.

Minimal negotiation scopes for either role:

```json
["catalogue:read","images:read","negotiations:read","negotiations:act","orders:read"]
```

Add `requests:write` for a buyer that creates requests. Add `products:write` and `images:write` only for a seller that manages its catalogue. Do not give administrator credentials to an autonomous negotiator.

The complete implementation contract and tests are documented in the [backend authorization guide](https://github.com/iivko/crossroads-backend/blob/main/docs/AUTHORIZATION.md).
