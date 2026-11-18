"""Local, read-only research dashboard. Never constructs Engine or opens CLI state."""
from __future__ import annotations

import hashlib
import gzip
import base64
import hmac
import io
import json
import mimetypes
import math
import re
import sqlite3
import time
import threading
import zipfile
import yaml
from . import server_status, studies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ASSETS = Path(__file__).with_suffix('')
FIELDS = {
    'digests': 'id project key topic title period_start period_end summary text sections version created updated',
    'documentation_focus': 'id project study_id cycle',
    'document_packages': 'id project study_key label report_pdf paper_pdf detailed_pdf task session submitted_at review_status',
    'studies': 'id study_id project title question status_note execution verification task_ids verification_task technical_record source_record report_pdf paper_pdf detailed_pdf story_draft figures consistency_record qa_record known_gaps review_notes documentation checks revision updated published_at',
    'scheduler': 'id project budget_usage updated',
    'research_stories': 'id project text time',
    'projects': 'id name folder mode purpose brief status created',
    'channels': 'id name project active',
    'tasks': 'id study_id work_kind document_phase document_kind document_cycle project channel thread role goal acceptance status created updated parent depends priority model effort summary next_steps notes verdict verification_target requires_verification not_before defer_reason last_error report budget attempts launch_hold',
    'sessions': 'id project task role status created started finished provider requested_allocation model effort account history_parent seconds reasons report stop_reason preempted',
    'messages': 'id project channel thread text time created user',
    'outbox': 'id channel thread text timestamp created status title',
    'jobs': 'id study_id project task session name status created started ended hours gpus gpu_mode gpu_memory_mib allocation cpu_threads seeds command exit_code stop_kind stop_reason stop_at launch_hold',
    'papers': 'canonical_id title authors year url version read_depth sections_read method results limitations supporting_sections checked_by task updated',
    'paper_links': 'id project paper relevance',
    'decisions': 'id project text sources disagreement time thread',
    'project_notes': 'id project text path time',
    'meetings': 'id project origin question turn max_turns status reports',
    'requests': 'id project thread reason priority kind status created session',
    'wakeups': 'id project thread reason at status created recurrence timezone last_fired',
}
REPORT_FIELDS = 'session task role model effort created status summary findings limitations evidence next_steps verdict reply resolved'
SUFFIXES = {'.md', '.txt', '.json', '.jsonl', '.csv', '.log', '.yaml', '.yml', '.png', '.jpg', '.jpeg', '.webp', '.pdf', '.py'}
BLOCKED = {'control', 'tmp', 'node_modules', '__pycache__'}


def pick(value, fields):
    return {key: value[key] for key in fields.split() if key in value}


def json_safe(value):
    """Represent undefined numeric measurements as null, never invalid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def read_json(path, limit=2_000_000):
    try:
        if path.is_file() and path.stat().st_size <= limit:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        pass
    return {}


class Dashboard:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.refresh_lock = threading.Lock()
        self.last_usage_refresh = 0

    def request_usage_refresh(self):
        # Request metadata polling through the daemon; never open credentials here.
        with self.refresh_lock:
            if read_json(self.root/'runtime/daemon.json').get('status') != 'running':
                return 503, {'error': 'Start the lab daemon to refresh account usage.'}
            marker = self.root/'runtime/subscription-refresh.request'
            if marker.exists():
                return 202, {'requested_at': marker.stat().st_mtime}
            if time.monotonic()-self.last_usage_refresh < 60:
                return 429, {'error': 'Account refresh was requested recently. Wait one minute.'}
            marker.touch(mode=0o600)
            self.last_usage_refresh = time.monotonic()
            return 202, {'requested_at': marker.stat().st_mtime}

    def request_renewal(self, account):
        cfg = yaml.safe_load((self.root/'config.yaml').read_text()) or {}
        if account not in [a['id'] for a in cfg.get('claude', {}).get('accounts', [])] or not re.fullmatch(r'[A-Za-z0-9_-]+', account):
            return 404, {'error': 'Unknown account'}
        with self.refresh_lock:
            if read_json(self.root/'runtime/daemon.json').get('status') != 'running':
                return 503, {'error': 'Start the lab daemon first.'}
            marker = self.root/'runtime'/('subscription-renew.'+account+'.request')
            if not marker.exists():
                marker.touch(mode=0o600)
            return 202, {'requested_at': marker.stat().st_mtime}

    def account_usage(self, settings, sessions, now):
        # Read only config and allowlisted database metadata, never CLI credentials.
        try:
            cfg = yaml.safe_load((self.root / 'config.yaml').read_text()) or {}
        except (OSError, ValueError, yaml.YAMLError):
            cfg = {}
        names = [a['id'] for a in cfg.get('claude', {}).get('accounts', [])]
        if not names:
            names = sorted(k.split(':', 1)[1] for k in settings if k.startswith('subscription:'))
        named = bool(names)
        if not names and 'subscription' in settings:
            names = ['default']
        result = []
        login_state = settings.get('account_logins', {})
        max_age = cfg.get('subscription', {}).get('max_age_seconds', 900)
        for name in names:
            suffix = ':' + name if named else ''
            state = settings.get('subscription' + suffix, {})
            observed = state.get('observed_at')
            age = max(0, int(now - observed)) if observed else None
            status = 'unavailable' if age is None else 'stale' if age > max_age else 'ok'
            windows = {}
            for key, raw in state.get('windows', {}).items():
                if key not in {'five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet', 'seven_day_oauth_apps'}:
                    continue
                used = raw.get('used_percentage')
                if not isinstance(used, (int, float)) or not 0 <= used <= 100:
                    continue
                windows[key] = dict(used_percentage=used, remaining_percentage=100-used,
                                    resets_at=raw.get('resets_at'))
            cooldown = settings.get('cooldown' + suffix, {})
            login = login_state.get('logins', {}).get(name)
            login_age = now - login_state.get('updated', 0)
            login = login if isinstance(login, bool) and login_age <= max_age else None
            required = bool(settings.get('authentication' + suffix, {}).get('required'))
            result.append(dict(id=name, status=status, observed_at=observed, age_seconds=age,
                windows=windows, metadata_error=bool(state.get('error')), renewal_needed=state.get('error_kind') == 'credentials' and 'needs refreshing' in state.get('error', ''), renewal=pick(settings.get('subscription_renewal:'+name, {}), 'status message updated'), login_usable=login,
                auth_required=required, cooldown_until=cooldown.get('until', 0) if cooldown.get('until', 0) > now else None,
                active_sessions=sum(s.get('account', 'default') == name and s.get('status') in ('running', 'stopping') for s in sessions)))
        return result

    def snapshot(self):
        data = {kind: [] for kind in FIELDS}
        data.update(research_chat_sessions=[],subscription_refresh={},server=server_status.view({}),accounts=[], reports=[], files=[], helpers=[], checkpoints=[], events=[], fetched_at=time.time(), warnings=[])
        try:
            cfg = yaml.safe_load((self.root/'config.yaml').read_text()) or {}
        except (OSError, ValueError, yaml.YAMLError):
            cfg = {}
        data['agent_backend']=dict(provider='claude',model=cfg.get('claude',{}).get('supervisor_model','opus'),
            effort=cfg.get('claude',{}).get('supervisor_effort','high'))
        policy=cfg.get('documentation',{})
        data['documentation_policy']={k:policy[k] for k in ('enabled','report_format',
            'max_task_sessions','max_turns','writer_model_ceiling','writer_effort_ceiling') if k in policy}
        database = self.root / 'runtime/lab.sqlite3'
        if not database.exists():
            data['warnings'].append('No lab database yet. Start the research daemon to create project records.')
            return data, {}
        # A consistent read transaction, with no migrations, writes, or scheduler initialization.
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            raw = {kind: [] for kind in FIELDS}
            settings = {}
            for kind, record_id, payload in conn.execute('SELECT kind,id,data FROM records'):
                if kind == 'research_chats':
                    chat_record=json.loads(payload)
                    if chat_record.get('status')=='running':
                        from .runner import identity
                        if chat_record.get('owner_identity')==identity(chat_record.get('owner_pid')):
                            data['research_chat_sessions'].append(pick(chat_record,'id project title status account model effort'))
                if kind == 'project_notes':
                    note = json.loads(payload)
                    if note.get('path') == 'notes/research-story.md':
                        raw['research_stories'].append(dict(note, id=record_id))
                if kind == 'settings':
                    value = json.loads(payload)
                    name = value.get('id', '')
                    if name.startswith('subscription_renewal:') or name in {'pause', 'subscription_refresh', 'server_status', 'account_logins', 'subscription', 'cooldown', 'authentication'} or name.startswith(('subscription:', 'cooldown:', 'authentication:')):
                        settings[name] = value
                if kind in FIELDS:
                    value = json.loads(payload)
                    raw[kind].append(value)
            for kind, rows in raw.items():
                data[kind] = [pick(row, FIELDS[kind]) for row in rows]
            for session, source in zip(data['sessions'], raw['sessions']):
                usage = source.get('usage')
                if isinstance(usage, dict):
                    session['usage'] = {k: v for k, v in usage.items()
                        if k in {'input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'}
                        and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < float('inf')}
            for eid, stamp, action, payload in conn.execute('SELECT id,time,action,data FROM events ORDER BY id DESC LIMIT 2000'):
                detail = json.loads(payload)
                data['events'].append(dict(id=eid, time=stamp, action=action,
                    **pick(detail, 'project task session job role type parent approved')))
        data['maintenance_hold']=pick(settings.get('pause',{}),'paused reason since')
        data["subscription_refresh"] = pick(settings.get("subscription_refresh", {}), "requested_at completed_at")
        data["server"] = server_status.view(settings.get("server_status", {}), data["fetched_at"])
        data["accounts"] = self.account_usage(settings, data["sessions"]+data["research_chat_sessions"], data["fetched_at"])
        # Outbox rows inherit project ownership from their channel, never from arbitrary text.
        channels = {c['id']: c.get('project') for c in data['channels']}
        for row in data['outbox']:
            row['project'] = channels.get(row.get('channel'))
        paths = {}
        for project in data['projects']:
            folder = project.get('folder', '')
            base = (self.root / 'projects' / folder).resolve()
            if not folder or not base.is_relative_to(self.root / 'projects') or base == self.root / 'projects':
                continue
            count = 0
            for section in ('notes', 'decisions', 'experiments', 'reports', 'runs'):
                if count >= 2000:
                    break
                for path in sorted((base / section).rglob('*')):
                    rel = path.relative_to(base)
                    if any(p.startswith('.') or p in BLOCKED for p in rel.parts):
                        continue
                    if path.suffix.lower() not in SUFFIXES or path.is_symlink() or not path.is_file():
                        continue
                    resolved = path.resolve()
                    if not resolved.is_relative_to(base):
                        continue
                    if count >= 2000:
                        data['warnings'].append('File listing limited to 2,000 files per project.')
                        break
                    count += 1
                    stat = path.stat()
                    fid = hashlib.sha256(str(path.relative_to(self.root)).encode()).hexdigest()[:24]
                    paths[fid] = resolved
                    data['files'].append(dict(id=fid, project=project['id'], name=path.name,
                        path=str(rel), section=section, size=stat.st_size, modified=stat.st_mtime))
                    if section == 'reports' and len(rel.parts) == 3 and path.name == 'report.json':
                        report = read_json(path)
                        if report:
                            data['reports'].append(dict(pick(report, REPORT_FIELDS), id=rel.parts[1],
                                project=project['id'], file=fid))
                    if section == 'runs' and len(rel.parts) == 3 and path.name == 'report.json':
                        report = read_json(path)
                        job = next((j for j in data['jobs'] if j['id'] == rel.parts[1]), None)
                        if job:
                            job.update(pick(report, 'summaries metrics_error'))
                            if isinstance(report.get('metrics'), dict):
                                job['metrics'] = report['metrics']
        # Story evidence must not disappear when a large project's general file
        # listing reaches its cap. Resolve only explicitly referenced local files,
        # using the same project boundaries and file-type restrictions.
        indexed = {f['id'] for f in data['files']}
        evidence_sources = list(data['research_stories']) + list(data['digests'])
        task_states = {t['id']: t.get('status') for t in data['tasks']}
        for package in data['document_packages']:
            package['review_status'] = task_states.get(package.get('task'), 'unknown')
            evidence_sources.append(dict(project=package['project'],text=' '.join('[document]('+package[k]+')' for k in ('report_pdf','paper_pdf','detailed_pdf') if package.get(k))))
        for study in data['studies']:
            project = next((p for p in data['projects'] if p['id'] == study['project']), None)
            if project:
                study.update(studies.current_view(self.root, project, study))
            links = [study.get(key) for key in studies.PATH_FIELDS] + study.get('figures', [])
            evidence_sources.append(dict(project=study['project'], text=' '.join('[file]('+path+')' for path in links if path)))
        for story in evidence_sources:
            project = next((p for p in data['projects'] if p['id'] == story.get('project')), None)
            if not project or not project.get('folder'):
                continue
            base = (self.root/'projects'/project['folder']).resolve()
            if not base.is_relative_to(self.root/'projects') or base == self.root/'projects':
                continue
            targets = dict.fromkeys(re.findall(r'!?\[[^\]]*\]\(([^\s)]+)\)', story.get('text', '')))
            for target in list(targets)[:200]:
                path = base/target
                resolved = path.resolve()
                if not resolved.is_relative_to(base) or path.is_symlink() or not path.is_file():
                    continue
                rel = resolved.relative_to(base)
                if not rel.parts or rel.parts[0] not in {'notes','decisions','experiments','reports','runs'}:
                    continue
                if any(part.startswith('.') or part in BLOCKED for part in rel.parts) or path.suffix.lower() not in SUFFIXES:
                    continue
                fid = hashlib.sha256(str(resolved.relative_to(self.root)).encode()).hexdigest()[:24]
                if fid in indexed:
                    continue
                stat = resolved.stat()
                indexed.add(fid); paths[fid] = resolved
                data['files'].append(dict(id=fid, project=project['id'], name=resolved.name,
                    path=rel.as_posix(), section=rel.parts[0], size=stat.st_size, modified=stat.st_mtime))
        # Only selected checkpoint/helper metadata from known assignment workspaces.
        for task in data['tasks']:
            sessions = [s for s in raw['sessions'] if s.get('task') == task['id']]
            if not sessions:
                continue
            source = max(sessions, key=lambda s: s.get('created', 0))
            workspace = Path(source.get('workspace', '/')).resolve()
            if not workspace.is_relative_to(self.root / 'workspaces'):
                continue
            checkpoint_path = (workspace / 'checkpoint.json').resolve()
            checkpoint = read_json(checkpoint_path) if checkpoint_path.is_relative_to(workspace) else {}
            if checkpoint:
                data['checkpoints'].append(dict(pick(checkpoint, 'completed next_step blocked files saved_at'), task=task['id']))
        seen_helpers = set()
        for source in raw['sessions']:
            owner = next((t for t in data['tasks'] if t['id'] == source.get('task')), None)
            if not owner:
                continue
            workspace = Path(source.get('workspace', '/')).resolve()
            if not workspace.is_relative_to(self.root / 'workspaces'):
                continue
            state_path = (workspace / 'control/native-state.json').resolve()
            state = read_json(state_path) if state_path.is_relative_to(workspace) else {}
            agents = state.get('agents', {})
            if isinstance(agents, dict):
                for aid, agent in agents.items():
                    if not isinstance(agent, dict):
                        continue
                    helper = dict(pick(agent, 'type session status updated summary'), id=aid, task=owner['id'], project=owner['project'])
                    session = next((s for s in data['sessions'] if s['id'] == helper.get('session')), {})
                    if helper.get('status') == 'running' and session.get('status') != 'running':
                        helper['status'] = 'interrupted'
                    key = (helper.get('session'), aid)
                    if key not in seen_helpers:
                        seen_helpers.add(key)
                        data['helpers'].append(helper)
        return data, paths


def story_pdf_archive(data, paths, project_id, selected=None):
    """Bundle only current study PDFs from one project, using the file allowlist."""
    if not any(p['id'] == project_id for p in data['projects']):
        raise ValueError('Choose an available project')
    files = {f['path']: f for f in data['files'] if f['project'] == project_id}
    available = {}
    for index, study in enumerate(s for s in data['studies'] if s['project'] == project_id):
        folder = re.sub(r'[^a-zA-Z0-9_-]+', '-', study.get('title', '')).strip('-')[:90] or 'study'
        for key, name in (('paper_pdf', 'paper-section.pdf'), ('detailed_pdf', 'detailed-report.pdf')):
            file = files.get(study.get(key))
            if file and file['path'].lower().endswith('.pdf') and file['id'] in paths:
                available.setdefault(file['id'], (paths[file['id']], f'{index+1:02d}-{folder}/{name}'))
    ids = list(dict.fromkeys(selected)) if selected is not None else list(available)
    if not ids or len(ids) > 100 or any(fid not in available for fid in ids):
        raise ValueError('Select between 1 and 100 current study PDFs from this project')
    sizes = [available[fid][0].stat().st_size for fid in ids]
    if any(size > 20_000_000 for size in sizes) or sum(sizes) > 200_000_000:
        raise ValueError('Download is limited to 20 MB per PDF and 200 MB per ZIP; select fewer PDFs')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        for fid in ids:
            path, name = available[fid]
            archive.write(path, name)
    return output.getvalue()


def make_server(root, port=8765):
    dashboard = Dashboard(root)
    from .research_chat import ChatService, view as chat_view

    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, body, content_type='application/json; charset=utf-8', attachment=None):
            # Browser refreshes otherwise transfer several MB of repeated text
            # through SSH/editor forwarding. Keep binary downloads untouched.
            compress = False
            for item in self.headers.get('Accept-Encoding', '').lower().split(','):
                parts = [part.strip() for part in item.split(';')]
                if parts[0] != 'gzip':
                    continue
                try:
                    quality = next((float(part[2:]) for part in parts[1:] if part.startswith('q=')), 1)
                    compress = 0 < quality <= 1
                except ValueError:
                    compress = False
                break
            compress = compress and len(body) >= 1024 and (
                content_type.startswith('text/') or content_type.split(';')[0] in
                {'application/json', 'application/javascript'})
            if compress:
                body = gzip.compress(body, compresslevel=1)
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Vary', 'Accept-Encoding')
            if compress:
                self.send_header('Content-Encoding', 'gzip')
            if attachment:
                self.send_header('Content-Disposition', f'attachment; filename="{attachment}"')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; font-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def local_request(self):
            host = self.headers.get('Host', '')
            # SSH/editor forwarding can change the browser's loopback port.
            local_host = re.fullmatch(r'(?:127\.0\.0\.1|localhost|\[::1\])(?::([0-9]{1,5}))?', host)
            valid_port = local_host and (not local_host[1] or 1 <= int(local_host[1]) <= 65535)
            origin = self.headers.get('Origin')
            if not valid_port or (origin and origin != 'http://' + host):
                self.respond(403, b'{"error":"Local access only"}')
                return False
            return True

        def notes_login(self):
            try:
                credentials=json.loads((Path(root)/'runtime/dashboard-access/credentials.json').read_text())
                header=self.headers.get('Authorization','')
                supplied=base64.b64decode(header[6:],validate=True) if header.startswith('Basic ') and len(header)<4096 else b''
                expected=(credentials['username']+':'+credentials['password']).encode()
                if hmac.compare_digest(supplied,expected):return True
            except (OSError,ValueError,KeyError):pass
            self.send_response(401);self.send_header('WWW-Authenticate','Basic realm="Research dashboard"');self.send_header('Content-Length','0');self.end_headers()
            return False

        def do_POST(self):
            if not self.local_request():
                return
            if self.path.startswith('/api/research-chat/'):
                if not self.notes_login():return
                try:
                    ident=self.path.removeprefix('/api/research-chat/')
                    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',ident):raise ValueError('Invalid workspace')
                    length=int(self.headers.get('Content-Length','0'))
                    if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type','').split(';')[0]!='application/json' or not 0<length<=1000000:raise ValueError('Use a JSON chat request smaller than 1 MB')
                    payload=json.loads(self.rfile.read(length))
                    if not isinstance(payload,dict):raise ValueError('Expected an object')
                    result=ChatService(root).action(ident,payload)
                    return self.respond(200,json.dumps(result).encode())
                except (ValueError,KeyError,TypeError) as error:return self.respond(400,json.dumps(dict(error=str(error))).encode())
                except (OSError,sqlite3.Error):return self.respond(503,b'{"error":"Chat could not start. Your browser draft is preserved."}')
            if self.path.startswith('/api/research-notes/'):
                if not self.notes_login():return
                from . import research_notes
                try:
                    if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type','').split(';')[0]!='application/json':
                        raise ValueError('Use a JSON request without transfer encoding')
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=12_000_000:raise ValueError('Invalid notes request size')
                    body=self.rfile.read(length)
                    payload=json.loads(body)
                    ident=self.path.removeprefix('/api/research-notes/')
                    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',ident):raise ValueError('Invalid project')
                    result=research_notes.human_action(root,ident,payload)
                    return self.respond(200,json.dumps(result).encode())
                except research_notes.Conflict as error:return self.respond(409,json.dumps(dict(error=str(error))).encode())
                except (ValueError,KeyError,TypeError) as error:return self.respond(400,json.dumps(dict(error=str(error))).encode())
                except (OSError,sqlite3.Error):return self.respond(503,b'{"error":"Notes could not be saved; keep your draft and retry."}')
            renewal = re.fullmatch(r'/api/subscription-renew/([A-Za-z0-9_-]+)', self.path)
            if self.path != '/api/subscription-refresh' and not renewal:
                return self.respond(405, b'{"error":"Read-only research dashboard"}')
            if self.headers.get('Content-Length', '0') != '0' or self.headers.get('Transfer-Encoding'):
                return self.respond(400, b'{"error":"No request body is accepted"}')
            try:
                code, body = dashboard.request_renewal(renewal[1]) if renewal else dashboard.request_usage_refresh()
                return self.respond(code, json.dumps(body).encode())
            except OSError:
                return self.respond(503, b'{"error":"Could not request account refresh"}')

        def do_GET(self):
            if not self.local_request():
                return
            url = urlsplit(self.path)
            try:
                if url.path=='/api/research-chat-asset':
                    if not self.notes_login():return
                    from .research_chat import database
                    from .chat_figures import ALLOWED
                    query=parse_qs(url.query);pid=query.get('project',[''])[0];aid=query.get('asset',[''])[0]
                    asset=database(root).get('research_chat_assets',aid)
                    if not asset or asset['project']!=pid:return self.respond(404,b'{"error":"Figure unavailable in this workspace"}')
                    path=Path(asset['path']);base=(Path(root)/'runtime/research-chat-assets'/pid).resolve()
                    if not path.resolve().is_relative_to(base) or path.is_symlink() or path.suffix.lower() not in ALLOWED or not path.is_file():return self.respond(404,b'{"error":"Figure unavailable"}')
                    name=re.sub(r'[^A-Za-z0-9._-]','-',asset['name'])[:160] if query.get('download')==['1'] else None
                    return self.respond(200,path.read_bytes(),ALLOWED[path.suffix.lower()],name)
                if url.path.startswith('/api/research-chat/'):
                    if not self.notes_login():return
                    ident=url.path.removeprefix('/api/research-chat/')
                    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',ident):raise ValueError('Invalid workspace')
                    query=parse_qs(url.query);cid=query.get('conversation',[None])[0]
                    if query.get('stream')==['1']:
                        initial=chat_view(root,ident,cid)
                        if not cid:raise ValueError('Select a conversation')
                        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-store');self.send_header('X-Accel-Buffering','no');self.end_headers()
                        last=None;heartbeat=time.monotonic();deadline=time.monotonic()+7200
                        try:
                            with sqlite3.connect(Path(root)/'runtime/lab.sqlite3',timeout=3) as conn:
                                while time.monotonic()<deadline:
                                    row=conn.execute("SELECT data FROM records WHERE kind='research_chats' AND id=?",(cid,)).fetchone()
                                    c=json.loads(row[0]) if row else {}
                                    if c.get('project')!=ident or c.get('deleted'):break
                                    state={k:c.get(k) for k in ('id','status','live_reply','activity','progress','error','account','model','effort','live_assets')};encoded=json.dumps(state)
                                    if encoded!=last:
                                        self.wfile.write(('data: '+encoded+'\n\n').encode());self.wfile.flush();last=encoded;heartbeat=time.monotonic()
                                    elif time.monotonic()-heartbeat>10:self.wfile.write(b': keepalive\n\n');self.wfile.flush();heartbeat=time.monotonic()
                                    if c.get('status')!='running':break
                                    time.sleep(.2)
                        except (BrokenPipeError,ConnectionResetError,OSError):pass
                        self.close_connection=True;return
                    return self.respond(200,json.dumps(chat_view(root,ident,cid)).encode())
                if url.path.startswith('/notes-assets/'):
                    base=ASSETS/'notes-vendor';path=(base/url.path.removeprefix('/notes-assets/')).resolve()
                    if not path.is_relative_to(base.resolve()) or not path.is_file() or path.suffix not in ('.js','.css','.woff2','.woff','.ttf'):
                        return self.respond(404,b'{"error":"Asset unavailable"}')
                    return self.respond(200,path.read_bytes(),mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
                if url.path.startswith('/api/research-notes/'):
                    from . import research_notes
                    args=parse_qs(url.query)
                    operation=url.path.removeprefix('/api/research-notes/')
                    ident=args.get('project',[''])[0] if operation in ('asset','pdf') else operation
                    database=research_notes.store(root)
                    with database.tx() as tx:
                        pid=ident
                        ident=research_notes.select(tx,pid,args.get('note_id',[None])[0])
                        note=research_notes.current(tx,ident)
                        if operation not in ('asset','pdf') and args.get('metadata'):
                            data=research_notes.view(tx,ident)
                            stamp=','.join(p['id'] for p in data['proposals'])+'|'+','.join(g['id'] for g in data['grants'])+'|'+json.dumps([(n['note_id'],n['title']) for n in data['notes']],ensure_ascii=False,separators=(',',':'))
                            return self.respond(200,json.dumps(dict(revision=note['revision'],proposal_stamp=stamp)).encode())
                        if operation=='asset':
                            asset=tx.get('research_note_assets',args.get('asset',[''])[0])
                            if not asset or asset['project']!=pid:return self.respond(404,b'{"error":"Figure unavailable"}')
                            return self.respond(200,Path(asset['path']).read_bytes(),asset['mime'])
                        if operation=='pdf':
                            revision=int(args.get('revision',[note['revision']])[0])
                            record=note if revision==note['revision'] else tx.get('research_note_revisions',ident+':'+str(revision))
                            if not record:raise ValueError('Revision unavailable')
                            figures=[a for a in tx.all('research_note_assets') if a['project']==pid]
                            title=note.get('title','Research Notes')
                        elif args.get('revision'):
                            old=tx.get('research_note_revisions',ident+':'+str(int(args['revision'][0])))
                            if not old:raise ValueError('Revision unavailable')
                            return self.respond(200,json.dumps(dict(note=old)).encode())
                        else:return self.respond(200,json.dumps(research_notes.view(tx,ident)).encode())
                    if operation=='pdf':
                        from .notes_pdf import export_pdf
                        try:pdf=export_pdf(Path(root),record,figures,title)
                        except Exception:return self.respond(503,b'{"error":"PDF export failed. Notes are saved; retry or contact the operator."}')
                        return self.respond(200,pdf,'application/pdf',re.sub(r'[^A-Za-z0-9_-]+','-',title).strip('-')[:100]+'.pdf')
                if url.path == '/api/snapshot':
                    data, _ = dashboard.snapshot()
                    return self.respond(200, json.dumps(json_safe(data), ensure_ascii=False, allow_nan=False).encode())
                if url.path == '/api/story-pdfs':
                    query = parse_qs(url.query, keep_blank_values=True, max_num_fields=102)
                    data, paths = dashboard.snapshot()
                    try:
                        archive = story_pdf_archive(data, paths, query.get('project', [''])[0], query.get('id'))
                    except ValueError as error:
                        return self.respond(400, json.dumps({'error': str(error)}).encode())
                    return self.respond(200, archive, 'application/zip', 'research-story-pdfs.zip')
                if url.path == '/api/file':
                    _, paths = dashboard.snapshot()
                    fid = parse_qs(url.query).get('id', [''])[0]
                    path = paths.get(fid)
                    if not path:
                        return self.respond(404, b'{"error":"Research file unavailable"}')
                    if path.stat().st_size > 20_000_000:
                        return self.respond(413, b'{"error":"File exceeds the 20 MB preview limit"}')
                    mime = mimetypes.guess_type(path.name)[0] or 'text/plain'
                    if path.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp', '.pdf'}:
                        mime = 'text/plain; charset=utf-8'
                    return self.respond(200, path.read_bytes(), mime)
                assets = {'/': 'index.html', '/app.js': 'app.js', '/help.js': 'help.js', '/style.css': 'style.css'}
                if url.path in assets:
                    path = ASSETS / assets[url.path]
                    return self.respond(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] + '; charset=utf-8')
                return self.respond(404, b'{"error":"Not found"}')
            except (OSError, sqlite3.Error, ValueError):
                return self.respond(503, b'{"error":"Records temporarily unavailable. Retry shortly."}')

        def log_message(self, format, *args):
            pass

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def serve(root, port=8765):
    server = make_server(root, port)
    print(f'Research dashboard: http://127.0.0.1:{server.server_port} (read-only)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
