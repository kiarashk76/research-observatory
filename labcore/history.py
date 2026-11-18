"""Account-independent archives of completed CLI conversations.

Credentials stay in account directories. Each continuation forks a terminal
session's transcript; no two lab sessions write the same conversation.
"""
import os
import json
import re
import shutil
import tempfile
from pathlib import Path

from . import accounts


def enabled(cfg):
    return cfg.get('claude', {}).get('shared_session_history', True)


def project_directory(engine, project):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', project):
        raise ValueError('Invalid history project ID')
    return engine.root / 'runtime' / 'claude-history' / project


def usable(path):
    try:
        has_messages = False
        with path.open() as transcript:
            for line in transcript:
                if line.strip():
                    entry = json.loads(line)
                    has_messages |= isinstance(entry, dict) and entry.get('type') in ('user', 'assistant') and isinstance(entry.get('message'), dict)
        return has_messages
    except (OSError, ValueError, UnicodeError):
        return False


def archive(engine, session):
    """Copy only a stopped, known session, without modifying its CLI files."""
    if session.get('provider', 'claude') != 'claude':
        return None
    if not enabled(engine.cfg) or session['status'] in ('running', 'stopping'):
        return None
    # Terminal database state is insufficient if a process has yet to exit.
    if session.get('pid'):
        from .runner import identity
        if session.get('identity') and identity(session['pid']) == session['identity']:
            return None
    sid = session['id']
    if not re.fullmatch(r'[A-Za-z0-9_-]+', sid):
        return None
    directory = project_directory(engine, session['project'])
    target = directory / (sid + '.jsonl')
    if target.is_file() and not target.is_symlink():
        return target
    if session.get('account'):
        if session['account'] not in {a['id'] for a in accounts.profiles(engine.cfg)}:
            return None
        roots = [accounts.directory(engine.cfg, session['account'])]
    else:
        roots = [engine.root / 'runtime/claude-auth/config']
    for root in roots:
        projects = root / 'projects'
        for source in projects.glob('*/' + sid + '.jsonl'):
            if source.is_symlink() or not source.resolve().is_relative_to(projects.resolve()) or not source.stat().st_size:
                continue
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(dir=directory, prefix='.' + sid)
            try:
                with os.fdopen(fd, 'wb') as out, source.open('rb') as original:
                    shutil.copyfileobj(original, out)
                if not usable(Path(temporary)):
                    return None
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return target
    return None


def candidates(engine, session):
    """History never crosses projects, roles, assignments, or Slack threads."""
    result = []
    from .documentation_policy import is_documentation
    task=engine.store.get('tasks',session.get('task',''),{})
    reset=engine.store.get('projects',session['project'],{}).get('writing_reset_at',0) if session['role']=='supervisor' or is_documentation(task) else 0
    for old in engine.store.all('sessions'):
        if old['id'] == session['id'] or old['status'] in ('running', 'stopping'):
            continue
        if old.get('provider', 'claude') != session.get('provider', 'claude'):
            continue
        if old['project'] != session['project'] or old['role'] != session['role']:
            continue
        if old['created'] < reset:
            continue
        if session.get('task'):
            if old.get('task') != session['task']:
                continue
        elif session['role'] != 'supervisor' or old.get('task') or (
                old.get('channel'), old.get('thread')) != (session.get('channel'), session.get('thread')):
            continue
        result.append(old)
    return sorted(result, key=lambda old: old['created'], reverse=True)


def prepare(engine, session):
    if not enabled(engine.cfg):
        return []
    from .documentation_policy import is_documentation
    task=engine.store.get('tasks',session.get('task',''),{})
    policy=engine.cfg.get('documentation',{})
    if policy.get('enabled',False) and is_documentation(task) and not policy.get('resume_cli_history',False):
        # Keep archives available, but resume document work from artifacts/checkpoints.
        # Long legacy conversations must not become every continuation's input.
        session['history_mode']='artifact_handoff'
        return []
    for old in candidates(engine, session):
        path = archive(engine, old)
        if path and usable(path):
            session.update(history_parent=old['id'], history_transcript=str(path))
            return ['--resume', str(path), '--fork-session', '--system-prompt-snapshot', 'off']
    return []


def collect(engine):
    if enabled(engine.cfg):
        for session in engine.store.all('sessions'):
            archive(engine, session)
