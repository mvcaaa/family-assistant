from datetime import datetime

from family_assistant.db import Store


def settle(store: Store, actor_id: int, approval_id: str, approve: bool, now: datetime) -> tuple[str, object]:
    owner = store.owner()
    if owner is None or int(owner["telegram_id"]) != actor_id:
        return "forbidden", None
    row = store.approval(approval_id)
    if row is None:
        return "missing", None
    decided = store.decide(approval_id, "approved" if approve else "denied", now)
    if decided is None:
        return "stale", row
    if approve and str(decided["kind"]) == "memory_replace":
        store.replace_memory(
            int(decided["target_user_id"]),
            str(decided["new_body"] or ""),
            "owner approved",
            now,
        )
    return ("approved" if approve else "denied"), decided
