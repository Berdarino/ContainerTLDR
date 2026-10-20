import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

HEARTBEAT = "heartbeat.json"  # the schedule loop writes when the next check is due, before it starts waiting
# How late a check may finish before the healthcheck fails; many summaries on a slow GPU take a while
GRACE = timedelta(hours=2)


def overdue(data_dir: Path) -> str | None:
    """Why the scheduled check looks stuck, or None while it's on time. For the healthcheck and /health."""
    try:
        due = datetime.fromisoformat(json.loads((data_dir / HEARTBEAT).read_text())["next_check"])
    except (OSError, ValueError, KeyError):
        return "no heartbeat yet"
    if datetime.now(UTC) > due + GRACE:
        return f"the check due at {due:%Y-%m-%d %H:%M %Z} hasn't finished"
    return None
