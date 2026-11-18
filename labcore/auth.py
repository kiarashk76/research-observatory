"""Detect restored local CLI login without model calls or storing credentials."""
import json
import time
from pathlib import Path
from . import accounts


def source_state(path=None):
    path = path or Path.home() / '.claude/.credentials.json'
    try:
        data = json.loads(path.read_text()).get('claudeAiOauth', {})
        now = time.time() * 1000
        usable = bool(data.get('accessToken')) and (data.get('expiresAt', 0) > now or
                    bool(data.get('refreshToken')) and (not data.get('refreshTokenExpiresAt') or data['refreshTokenExpiresAt'] > now))
        return dict(mtime_ns=path.stat().st_mtime_ns, usable=usable)
    except (OSError, ValueError, TypeError):
        return dict(mtime_ns=0, usable=False)


def recover(engine):
    for account in ([a["id"] for a in accounts.profiles(engine.cfg)] or [None]):
        _recover(engine, account)


def _recover(engine, account=None):
    state = engine.store.get('settings', accounts.key('authentication', account), {})
    if not state.get('required'):
        return
    source = source_state(accounts.directory(engine.cfg, account) / ".credentials.json" if account else None)
    if not source['usable'] or source['mtime_ns'] == state.get('source_mtime_ns'):
        return
    # Runner.environment imports the changed home login before its next launch.
    with engine.store.tx() as tx:
        current = tx.get('settings', accounts.key('authentication', account), {})
        if current.get('required'):
            current.update(required=False, restored_at=time.time())
            tx.put('settings', current)


def notice(engine, channel, thread, account=None):
    state = engine.store.get('settings', accounts.key('authentication', account), {})
    if state.get('required'):
        command = ('CLAUDE_CONFIG_DIR=' + str(accounts.directory(engine.cfg, account)) + ' claude auth login') if account else 'claude auth login'
        engine.store.outbox(channel, thread,
            'Claude CLI is signed out or its login expired. Your message and research files are saved. '
            f'On the server, run `{command}` and sign in with your Claude subscription. '
            'I’ll resume queued work automatically when the new login is detected; no Slack token or screen restart is needed.',
            key=accounts.key('authentication', account) + ':' + str(state.get('since')) + ':' + channel + ':' + str(thread))
