"""Best-effort subscription metadata, not token-to-quota estimates.

The account endpoint is used by Claude clients but is not a stable public API.
Only normalized usage/reset fields are persisted; authentication stays private.
"""
import datetime as dt
import json
import math
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo
from . import accounts, auth

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
WINDOWS = {"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "seven_day_oauth_apps"}


def epoch(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Reset times must include a timezone")
        result = result.timestamp()
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Invalid reset timestamp")
    return result


def normalize(payload):
    windows = {}
    for name in WINDOWS:
        entry = payload.get(name)
        if not isinstance(entry, dict) or entry.get("utilization") is None:
            continue
        value = float(entry["utilization"])
        if not math.isfinite(value) or not 0 <= value <= 100:
            continue
        try:
            reset = epoch(entry.get("resets_at"))
        except (ValueError, TypeError):
            reset = None
        windows[name] = {"used_percentage": value, "remaining_percentage": 100 - value, "resets_at": reset}
    if not windows:
        raise ValueError("The account returned no recognized subscription windows")
    return windows


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None  # Never forward OAuth credentials to another URL.


class CredentialError(ValueError):
    """Local login needs attention; no provider request was made."""


def fetch(cfg, account=None):
    paths = [Path(cfg["root"]) / "runtime/claude-auth/config/.credentials.json",
             Path.home() / ".claude/.credentials.json"]
    if account:
        paths = [accounts.directory(cfg, account) / ".credentials.json"]
    credentials = []
    for path in paths:
        try:
            data = json.loads(path.read_text()).get("claudeAiOauth", {})
            if data.get("accessToken"):
                credentials.append(data)
        except (OSError, ValueError):
            continue
    if not credentials:
        raise CredentialError("No local Claude subscription login is available")
    credential = max(credentials, key=lambda c: c.get("expiresAt", 0))
    if credential.get("expiresAt", 0) / 1000 <= time.time():
        raise CredentialError("Claude login needs refreshing; metadata is unavailable until normal CLI authentication refreshes")
    request = urllib.request.Request(ENDPOINT, headers={"Authorization": "Bearer " + credential["accessToken"],
        "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json", "User-Agent": "research-lab/2"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
        data = response.read(200000)
    return json.loads(data)


def view(engine, now=None, account=None):
    now = time.time() if now is None else now
    state = engine.store.get("settings", accounts.key("subscription", account), {})
    if account is None and accounts.profiles(engine.cfg):
        state = {}  # Legacy snapshots must not be attributed to a named login.
    observed = state.get("observed_at")
    age = now - observed if observed else None
    max_age = engine.cfg.get("subscription", {}).get("max_age_seconds", 900)
    status = "unavailable" if not observed else "stale" if age > max_age else "ok"
    zone = ZoneInfo(engine.cfg.get("timezone", "UTC"))
    windows = {}
    for name, raw in state.get("windows", {}).items():
        reset = raw.get("resets_at")
        windows[name] = dict(raw, seconds_to_reset=max(0, int(reset - now)) if reset else None,
                            resets_at_local=dt.datetime.fromtimestamp(reset, zone).isoformat() if reset else None,
                            reset_passed=bool(reset and reset <= now))
    result = {"status": status, "source": "Claude account usage metadata", "observed_at": observed,
            "age_seconds": int(age) if age is not None else None, "last_error": state.get("error"), "windows": windows,
            "note": "Percentages are account-wide snapshots, not exact forecasts of how much a task will consume."}
    if account is None and accounts.profiles(engine.cfg):
        result["accounts"] = {a["id"]: view(engine, now, a["id"]) for a in accounts.profiles(engine.cfg)}
    return result


def applicable(snapshot, model):
    names = {"five_hour", "seven_day", "seven_day_oauth_apps", "seven_day_" + model}
    return {k: v for k, v in snapshot.get("windows", {}).items() if k in names and not v.get("reset_passed")}


def gate(engine, model, usage_class="large", now=None, account=None):
    """Hold large work near a limit; hold all model work at actual exhaustion."""
    if account is None and accounts.profiles(engine.cfg):
        return accounts.choose(engine, model, usage_class, now)[1]
    policy = engine.cfg.get("subscription", {})
    if not policy.get("enabled", True):
        return None
    now = time.time() if now is None else now
    snapshot = view(engine, now, account)
    if snapshot["status"] != "ok":
        return None  # Unknown is never fabricated as full, empty, or a future reset.
    threshold = (policy.get("large_task_threshold_percent", 85) if usage_class == "large" else
                 policy.get("small_task_threshold_percent", 95) if usage_class == "small" else 100)
    blocked = {k: v for k, v in applicable(snapshot, model).items() if v["used_percentage"] >= threshold}
    if not blocked:
        return None
    known = all(v.get("resets_at") for v in blocked.values())
    # Every blocking window must reset; the earliest reset alone is insufficient.
    until = max(v["resets_at"] for v in blocked.values()) + policy.get("reset_buffer_seconds", 30) if known else now + policy.get("poll_seconds", 300)
    return {"reason": "subscription capacity reserved for smaller work" if usage_class == "large" and threshold < 100 else
                      "subscription capacity reserved for supervisor conversation" if threshold < 100 else "subscription window exhausted",
            "windows": list(blocked), "retry_at": until, "reset_known": known}


def display(engine, account=None):
    if account is None and accounts.profiles(engine.cfg):
        return "\n\n".join(a["id"] + ":\n" + display(engine, a["id"]) for a in accounts.profiles(engine.cfg))
    snapshot = view(engine, account=account)
    if not snapshot["windows"]:
        return "Subscription utilization is currently unavailable. " + (snapshot.get("last_error") or "No account snapshot has been fetched yet.")
    labels = {"five_hour": "Five-hour", "seven_day": "Weekly", "seven_day_opus": "Weekly Opus", "seven_day_sonnet": "Weekly Sonnet", "seven_day_oauth_apps": "Weekly Claude Code/apps"}
    lines = ["Claude subscription usage" + (" (stale snapshot)" if snapshot["status"] != "ok" else "")]
    for name, window in sorted(snapshot["windows"].items()):
        remaining = window["seconds_to_reset"]
        local_time = dt.datetime.fromtimestamp(window['resets_at'], ZoneInfo(engine.cfg.get('timezone', 'UTC'))).strftime('%b %d, %I:%M %p %Z') if window['resets_at'] else None
        reset = (f"resets {local_time} (in {remaining // 3600}h {(remaining % 3600) // 60}m)"
                 if remaining is not None and not window["reset_passed"] else "awaiting refreshed reset information")
        lines.append(f"• {labels.get(name, name)}: {window['used_percentage']:g}% used, {window['remaining_percentage']:g}% remaining; {reset}")
    lines.append(f"Snapshot age: {snapshot['age_seconds']} seconds. Account usage elsewhere also counts.")
    return "\n".join(lines)


class Monitor:
    def __init__(self, engine, fetcher=None):
        self.engine = engine
        self.fetcher = fetcher or fetch
        self.renewing = set()

    def request_renewals(self):
        for profile in accounts.profiles(self.engine.cfg):
            account = profile['id']
            marker = Path(self.engine.cfg['root'])/'runtime'/('subscription-renew.'+account+'.request')
            if not marker.exists():
                continue
            marker.unlink()
            if account in self.renewing:
                continue
            state = self.engine.store.get('settings', 'subscription_renewal:'+account, {})
            if time.time()-state.get('updated', 0) < 120:
                continue
            self.renewing.add(account)
            self.renewal_state(account, 'running', 'Renewing through one minimal Haiku request…')
            threading.Thread(target=self.renew, args=(account,), daemon=True).start()

    def renewal_state(self, account, status, message):
        with self.engine.store.tx() as tx:
            tx.put('settings', dict(id='subscription_renewal:'+account, status=status, message=message, updated=time.time()))

    def renew(self, account):
        try:
            env = dict(os.environ)
            for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR', 'CLAUDECODE', 'ANTHROPIC_BASE_URL'):
                env.pop(key, None)
            env.update(CLAUDE_CONFIG_DIR=str(accounts.directory(self.engine.cfg, account)), CLAUDE_CODE_MAX_RETRIES='0', API_TIMEOUT_MS='60000')
            binary = self.engine.cfg['claude']['bin']
            if '/' in binary and not Path(binary).is_absolute():
                binary = str(self.engine.root/binary)
            with tempfile.TemporaryDirectory(prefix='lab-auth-') as work:
                result = subprocess.run([binary, '-p', 'Reply only OK.', '--model', 'haiku', '--max-turns', '1',
                    '--tools', '', '--no-session-persistence', '--setting-sources', '',
                    '--settings', '{"disableAllHooks":true}', '--system-prompt', 'Reply only OK.'],
                    cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=90)
            if result.returncode:
                self.renewal_state(account, 'failed', 'CLI renewal failed. Check normal CLI authentication; browser login may be required.')
            else:
                self._refresh(True, account)
                self.renewal_state(account, 'done', 'CLI request succeeded. Usage was checked again; provider backoff may still apply.')
        except (OSError, subprocess.TimeoutExpired):
            self.renewal_state(account, 'failed', 'CLI renewal could not complete. Try normal CLI authentication.')
        finally:
            self.renewing.discard(account)

    def refresh(self, force=False):
        self.request_renewals()
        marker = Path(self.engine.cfg['root']) / 'runtime/subscription-refresh.request'
        try:
            requested_at = marker.stat().st_mtime
        except FileNotFoundError:
            requested_at = None
        for account in ([a["id"] for a in accounts.profiles(self.engine.cfg)] or [None]):
            if account not in self.renewing:
                self._refresh(force or requested_at is not None, account)
        if requested_at is not None:
            with self.engine.store.tx() as tx:
                tx.put('settings', dict(id='subscription_refresh', requested_at=requested_at, completed_at=time.time()))
            try:
                if marker.stat().st_mtime == requested_at:
                    marker.unlink()
            except FileNotFoundError:
                pass

    def _refresh(self, force=False, account=None):
        policy = self.engine.cfg.get("subscription", {})
        if not policy.get("enabled", True):
            return
        store = self.engine.store
        state = store.get("settings", accounts.key("subscription", account), {})
        now = time.time()
        credential_error = state.get("error_kind") == "credentials" or state.get("error") in {
            "No local Claude subscription login is available",
            "Claude login needs refreshing; metadata is unavailable until normal CLI authentication refreshes"}
        source = auth.source_state(accounts.directory(self.engine.cfg, account) / ".credentials.json" if account else None)
        login_changed = credential_error and source["usable"] and source["mtime_ns"] != state.get("source_mtime_ns")
        if state.get("retry_at", 0) > now and not (credential_error and (force or login_changed)):
            return
        if not force and not login_changed:
            reset_due = any(w.get("resets_at") and state.get("observed_at", 0) < w["resets_at"] <= now for w in state.get("windows", {}).values())
            if now - state.get("attempted_at", 0) < policy.get("poll_seconds", 300) and not reset_due:
                return
            if not policy.get('poll_when_idle', False):
                active_projects = {p['id'] for p in store.all('projects') if p['status'] == 'active'}
                active_channels = {c['id'] for c in store.all('channels') if c.get('active')}
                needed = (any(r['status'] == 'queued' and r.get('payload', {}).get('channel') in active_channels
                              and (r['project'] in active_projects or r.get('priority', 0) >= 100) for r in store.all('requests'))
                          or any(t['status'] == 'running' or t['project'] in active_projects and t['status'] in ('queued', 'deferred') for t in store.all('tasks')))
                if not needed:
                    return
        state.update(id=accounts.key("subscription", account), attempted_at=now, source_mtime_ns=source["mtime_ns"])
        try:
            windows = normalize(self.fetcher(self.engine.cfg, account=account) if account else self.fetcher(self.engine.cfg))
            state.update(windows=windows, observed_at=now, error=None, error_kind=None, retry_at=0)
        except (OSError, ValueError, TypeError) as e:
            delay = policy.get("poll_seconds", 300)
            if isinstance(e, urllib.error.HTTPError):
                try:
                    retry = int(e.headers.get("Retry-After", 0) or 0)
                except (ValueError, AttributeError):
                    retry = 0
                delay = max(delay, retry, 900 if e.code == 429 else 300)
                message = f"Account metadata HTTP {e.code}; the endpoint may be unavailable or changed"
            else:
                message = str(e) if isinstance(e, ValueError) else type(e).__name__
            state.update(error=message, error_kind="credentials" if isinstance(e, CredentialError) else "provider", retry_at=now + delay)
        with store.tx() as tx:
            tx.put("settings", state)
