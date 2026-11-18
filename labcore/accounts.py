"""Named Claude logins and deterministic, model-aware account selection."""
import re
import time
from pathlib import Path


def profiles(cfg):
    result = cfg.get("claude", {}).get("accounts", [])
    ids = [a.get("id", "") for a in result]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[a-zA-Z0-9_-]+", name) for name in ids):
        raise ValueError("Claude account IDs must be unique letters, digits, underscores or hyphens")
    return result


def directory(cfg, account):
    profile = next(a for a in profiles(cfg) if a["id"] == account)
    path = Path(profile["config_dir"]).expanduser()
    if not path.is_absolute():
        path = Path(cfg["root"]) / path
    return path.resolve()


def key(name, account=None):
    return name + ":" + account if account else name


def choose(engine, model, usage_class="large", now=None):
    from . import auth, subscription
    now = time.time() if now is None else now
    available, holds = [], []
    for profile in profiles(engine.cfg):
        account = profile["id"]
        state = engine.store.get("settings", key("authentication", account), {})
        cooldown = engine.store.get("settings", key("cooldown", account), {})
        if state.get("required") or not auth.source_state(directory(engine.cfg, account) / ".credentials.json")["usable"]:
            hold = dict(reason="Claude login needs refreshing", retry_at=now + 300, reset_known=False, windows=[])
        elif cooldown.get("until", 0) > now:
            hold = dict(reason="provider cooldown", retry_at=cooldown["until"], reset_known=cooldown.get("exact", False), windows=[])
        else:
            hold = subscription.gate(engine, model, usage_class, now, account=account)
        if hold:
            holds.append(dict(hold, account=account))
            continue
        snapshot = subscription.view(engine, now, account=account)
        windows = subscription.applicable(snapshot, model)
        known = snapshot["status"] == "ok" and bool(windows)
        remaining = min((w["remaining_percentage"] for w in windows.values()), default=0) if known else 0
        active = sum(s.get("account") == account and s["status"] in ("running", "stopping") for s in engine.store.all("sessions"))
        available.append(((known, remaining, -active), account))
    if available:
        return max(available, key=lambda item: item[0])[1], None
    hold = min(holds, key=lambda h: h["retry_at"]) if holds else dict(reason="No Claude accounts configured", retry_at=now + 300, reset_known=False, windows=[])
    return None, hold
