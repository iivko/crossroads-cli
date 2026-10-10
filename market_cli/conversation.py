"""Public transcript projection and safe plain-text terminal presentation."""

import unicodedata

PUBLIC_ACTIONS = {"message", "propose", "counter", "revise"}
PURPOSES = {"question", "answer", "tradeoff", "explanation"}


def terminal_text(value, limit=2000):
    """Neutralize terminal controls and bidi formatting in counterparty-authored text."""
    if not isinstance(value, str):
        return ""
    return "".join(
        char
        for char in value[:limit]
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )


def public_messages(room):
    """Project only explicitly public commercial messages, never private context."""
    result = []
    for activity in room.get("activity", []):
        data = activity.get("data", {})
        if activity.get("type") not in PUBLIC_ACTIONS or not isinstance(data.get("message"), str):
            continue
        if activity.get("role") not in {"buyer", "seller"}:
            continue
        item = {
            "id": activity["id"],
            "role": activity["role"],
            "type": activity["type"],
            "message": data["message"],
            "at": activity.get("at"),
        }
        if type(activity.get("run")) is int:
            item["run"] = activity["run"]
        if data.get("purpose") in PURPOSES:
            item["purpose"] = data["purpose"]
        for key in ("reply_to_action_id", "offer_id"):
            if isinstance(data.get(key), str):
                item[key] = data[key]
        if isinstance(data.get("image_ids"), list):
            item["image_ids"] = [value for value in data["image_ids"][:5] if isinstance(value, str)]
        result.append(item)
    return result
