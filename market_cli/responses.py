"""Validate the HTTP envelope while preserving the CLI's established JSON output."""

import re

# Only these endpoints previously returned bare arrays. Paginated collections keep
# their existing {items, limit, offset, ...} object instead of losing metadata.
RESPONSE_CONTRACTS = (
    ("GET", r"/health", "health", False),
    ("POST", r"/auth/token", "token", False),
    ("POST", r"/auth/logout", "session", False),
    ("GET", r"/auth/me", "identity", False),
    ("GET", r"/workspaces", "workspaces", True),
    ("GET", r"/credentials", "credentials", True),
    ("POST", r"/credentials", "credential", False),
    ("DELETE", r"/credentials/[^/]+", "credential", False),
    ("GET", r"/categories", "categories", True),
    ("GET", r"/products", "products", False),
    ("POST", r"/products", "product", False),
    ("GET", r"/products/[^/]+", "product", False),
    ("PUT", r"/products/[^/]+", "product", False),
    ("PUT", r"/products/[^/]+/stock", "product", False),
    ("GET", r"/products/[^/]+/history", "product_history", True),
    ("GET", r"/products/[^/]+/images", "images", True),
    ("POST", r"/products/[^/]+/images", "image", False),
    ("PUT", r"/products/[^/]+/images/cover", "product", False),
    ("PUT", r"/products/[^/]+/images/order", "product", False),
    ("DELETE", r"/products/[^/]+/images/[^/]+", "image", False),
    ("GET", r"/rules", "rules", False),
    ("PUT", r"/rules", "rules", False),
    ("GET", r"/rules/history", "rule_history", True),
    ("GET", r"/requests", "requests", False),
    ("POST", r"/requests", "request", False),
    ("GET", r"/requests/[^/]+", "request_detail", False),
    ("PUT", r"/requests/[^/]+", "request", False),
    ("POST", r"/requests/[^/]+/launch", "request", False),
    ("POST", r"/requests/[^/]+/cancel", "request", False),
    ("GET", r"/requests/[^/]+/matches", "matches", True),
    ("GET", r"/requests/[^/]+/compare", "comparison", False),
    ("GET", r"/negotiations", "negotiations", False),
    ("GET", r"/negotiations/[^/]+", "negotiation", False),
    ("GET", r"/negotiations/[^/]+/context", "context", False),
    ("POST", r"/negotiations/[^/]+/claim", "claim", False),
    ("POST", r"/negotiations/[^/]+/actions", "action", False),
    ("POST", r"/negotiations/[^/]+/retry", "negotiation", False),
    ("GET", r"/approvals/[^/]+", "negotiation", False),
    ("GET", r"/orders", "orders", False),
    ("GET", r"/orders/[^/]+", "order", False),
    ("GET", r"/activity", "activity", False),
    ("GET", r"/agent/instructions", "instructions", False),
    ("GET", r"/agent/inbox", "inbox", False),
)


def response_contract(method: str, path: str) -> tuple[str, bool] | None:
    for verb, pattern, label, legacy_array in RESPONSE_CONTRACTS:
        if method == verb and re.fullmatch(pattern, path):
            return label, legacy_array
    return None
