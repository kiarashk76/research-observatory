"""Durable one-shot and weekly reviews. Clock checks never invoke a model."""
import time
import datetime as dt
from zoneinfo import ZoneInfo

from .store import uid
from .subscription import epoch


def when(action, now=None):
    now = time.time() if now is None else now
    if action.get("at"):
        value = epoch(action["at"])
    elif action.get("delay_minutes") is not None:
        value = now + float(action["delay_minutes"]) * 60
    else:
        raise ValueError("Supply an ISO timestamp with timezone in at, or delay_minutes")
    if value is None or not now + 60 <= value <= now + 30 * 86400:
        raise ValueError("Schedule a one-shot wake between one minute and 30 days from now")
    return value


def schedule(tx, session, action):
    project = tx.get("projects", session["project"])
    if not project["human_started"]:
        raise ValueError("Do not schedule autonomous research wakes before a human starts the project")
    reason = str(action.get("reason", "")).strip()
    if not reason:
        raise ValueError("A scheduled wake needs a concrete purpose")
    at = when(action)
    recurrence = action.get('recurrence', 'once')
    if recurrence not in ('once', 'weekly'):
        raise ValueError('Scheduled reviews support once or weekly recurrence')
    timezone = action.get('timezone', 'UTC')
    ZoneInfo(timezone)  # Reject invalid zones before committing a schedule.
    key = str(action.get("key") or reason)
    pending = [w for w in tx.all("wakeups") if w["project"] == session["project"] and w["status"] == "pending"]
    existing = next((w for w in pending if w["key"] == key and w["thread"] == session.get("thread")), None)
    if not existing and len(pending) >= 5:
        raise ValueError("At most five pending wake-ups per project; update or cancel an existing one")
    wake = existing or {"id": uid(), "project": session["project"], "channel": session["channel"], "thread": session.get("thread"), "key": key}
    wake.update(at=at, reason=reason, status="pending", created=time.time(), recurrence=recurrence, timezone=timezone)
    tx.put("wakeups", wake)
    return wake


def fire(engine, now=None):
    now = time.time() if now is None else now
    with engine.store.tx() as tx:
        for wake in tx.all("wakeups"):
            if wake["status"] != "pending" or wake["at"] > now:
                continue
            channel = tx.get("channels", wake["channel"], {})
            project = tx.get("projects", wake["project"], {})
            if not channel.get("active") or tx.get("closed_threads", wake["channel"] + ":" + str(wake["thread"])):
                wake["status"] = "cancelled"
            elif project.get("status") != "active":
                continue
            else:
                weekly = wake.get('recurrence') == 'weekly'
                previous = tx.get('requests', wake.get('last_request_id', ''), {})
                if weekly and previous.get('status') in ('queued', 'running', 'blocked'):
                    continue  # Do not accumulate duplicate research requests during a hold.
                key = "wake:" + wake["id"] + (":" + str(int(wake['at'])) if weekly else '')
                if not tx.get("requests", key):
                    tx.put("requests", {"id": key, "project": wake["project"], "thread": wake["thread"], "kind": "supervisor",
                        "reason": ("Weekly scheduled review (coalesce missed weeks; cover the latest week): " if weekly else "Scheduled review: ") + wake["reason"], "priority": 65, "status": "queued", "created": now,
                        "payload": {"channel": wake["channel"], "wake": wake["id"]}})
                wake['last_request_id'] = key
                wake['last_fired'] = now
                if weekly:
                    at = dt.datetime.fromtimestamp(wake['at'], ZoneInfo(wake.get('timezone', 'UTC')))
                    while at.timestamp() <= now:
                        at += dt.timedelta(days=7)
                    wake['at'] = at.timestamp()
                else:
                    wake["status"] = "fired"
            tx.put("wakeups", wake)
