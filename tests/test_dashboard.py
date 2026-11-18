"""Read-only dashboard integration and file boundary tests; no models or Slack."""
import hashlib
import gzip
import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from labcore.dashboard import Dashboard, make_server, json_safe


class DashboardTests(unittest.TestCase):
    def test_dashboard_displays_claude_supervisor_profile(self):
        (self.root/'config.yaml').write_text('claude:\n  supervisor_model: opus\n  supervisor_effort: high\n')
        data,_=Dashboard(self.root).snapshot()
        self.assertEqual(data['agent_backend'],dict(provider='claude',model='opus',effort='high'))
        self.assertNotIn('codex_usage',data)

    def test_table_view_omits_old_story_versions_and_editorial_history(self):
        (self.root/'config.yaml').write_text('research_story:\n  format: study_table\n')
        with sqlite3.connect(self.db) as c:
            self.add(c,'note_history',dict(id='old',project='p',path='notes/research-story.md',text='Old narrative'))
            self.add(c,'story_reviews',dict(id='review',project='p',result={'summary':'Old editorial feedback'}))
        data,_=Dashboard(self.root).snapshot()
        self.assertNotIn('story_versions',data)
        self.assertNotIn('story_reviews',data)

    def test_maintenance_hold_is_visible_without_other_setting_fields(self):
        with sqlite3.connect(self.db) as c:
            self.add(c,'settings',dict(id='pause',paused=True,reason='Fix report workflow',since='now',token='private'))
        data,_=Dashboard(self.root).snapshot()
        self.assertEqual(data['maintenance_hold'],dict(paused=True,reason='Fix report workflow',since='now'))
        self.assertNotIn('private',json.dumps(data['maintenance_hold']))

    def test_unified_report_submission_is_indexed_without_a_pdf_pair(self):
        self.write('reports/new/Study_study_report.pdf','Draft PDF bytes')
        with sqlite3.connect(self.db) as c:
            self.add(c,'document_packages',dict(id='p:study',project='p',task='t',report_pdf='reports/new/Study_study_report.pdf'))
        data,paths=Dashboard(self.root).snapshot()
        self.assertEqual(data['document_packages'][0]['report_pdf'],'reports/new/Study_study_report.pdf')
        self.assertTrue(any(f['path']=='reports/new/Study_study_report.pdf' for f in data['files']))

    def test_story_evidence_survives_general_file_listing_cap(self):
        for number in range(2001):
            (self.base/'notes'/f'n{number:04}.md').write_text('Note')
        self.write('reports/zz/figure.png', 'Figure')
        with sqlite3.connect(self.db) as c:
            self.add(c, 'project_notes', dict(id='story', project='p', path='notes/research-story.md',
                text='![Result](reports/zz/figure.png) [private](../../runtime/lab.sqlite3)'))
        data, paths = Dashboard(self.root).snapshot()
        figure = next(f for f in data['files'] if f['path'] == 'reports/zz/figure.png')
        self.assertEqual(paths[figure['id']], self.base/'reports/zz/figure.png')
        self.assertFalse(any(path == self.db for path in paths.values()))

    def test_story_current_and_revision_history_are_project_scoped(self):
        with sqlite3.connect(self.db) as c:
            self.add(c, 'project_notes', dict(id='p:story', project='p', path='notes/research-story.md', text='Current story', time='2026-10-31'))
            self.add(c, 'note_history', dict(id='revision1', project='p', path='notes/research-story.md', text='Earlier story', time='2026-10-30'))
            self.add(c, 'project_notes', dict(id='private', project='p', path='notes/private.md', text='Unrelated private note'))
        data, _ = Dashboard(self.root).snapshot()
        self.assertEqual(data['research_stories'][0]['text'], 'Current story')
        self.assertNotIn('story_versions',data)
        self.assertNotIn('Unrelated private note', json.dumps(data['research_stories']))

    def test_nonfinite_metrics_are_null_in_browser_json(self):
        metrics = {'runs': [{'loss': float('nan'), 'ratio': float('inf'),
                             'negative': float('-inf'), 'accuracy': 0.9}]}
        encoded = json.dumps(json_safe(metrics), allow_nan=False)
        self.assertEqual(json.loads(encoded), {'runs': [dict(loss=None, ratio=None,
                                                            negative=None, accuracy=0.9)]})
        self.assertNotEqual(metrics['runs'][0]['loss'], metrics['runs'][0]['loss'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'runtime').mkdir()
        self.db = self.root / 'runtime/lab.sqlite3'
        with sqlite3.connect(self.db) as c:
            c.executescript('CREATE TABLE records(kind TEXT,id TEXT,data TEXT); CREATE TABLE events(id INTEGER,time TEXT,action TEXT,data TEXT);')
            self.add(c, 'projects', dict(id='p', folder='study', name='Study', status='active'))
            self.add(c, 'projects', dict(id='p2', folder='../../runtime', name='Bad folder'))
            self.add(c, 'tasks', dict(id='t', project='p', role='engineer', goal='Build', status='running'))
            self.add(c, 'channels', dict(id='c', project='p'))
            self.add(c, 'outbox', dict(id='o', channel='c', text='Saved reply', status='sent'))
            self.add(c, 'sessions', dict(id='s', project='p', task='t', status='done',
                workspace=str(self.root/'workspaces/p/t'), stdout='/private', identity='private', error='private'))
            self.add(c, 'jobs', dict(id='j', project='p', task='t', name='Run', status='failed'))
            self.add(c, 'settings', dict(id='secret', token='do-not-expose'))
        self.base = self.root / 'projects/study'
        self.write('reports/s/report.json', dict(session='s',task='t',role='engineer',summary='Result',actions=[{'secret':True}],evidence=[]))
        self.write('runs/j/report.json', dict(metrics='workspace/path',metrics_error='No valid metrics'))
        self.write('notes/note.md', '# Note\nEvidence')
        self.write('notes/control/secret.json', {'token':'do-not-expose'})
        self.write('notes/.private/secret.json', {'token':'do-not-expose'})
        (self.base/'notes/link.json').symlink_to(self.db)
        (self.base/'notes/private-dir').symlink_to(self.root/'runtime', target_is_directory=True)
        workspace=self.root/'workspaces/p/t'
        (workspace/'control').mkdir(parents=True)
        (workspace/'checkpoint.json').write_text(json.dumps(dict(completed=['Built'],next_step='Review',saved_at=1)))
        (workspace/'control/native-state.json').write_text(json.dumps(dict(agents={'h':dict(type='lab-reviewer',session='s',status='running',summary='Reviewed',transcript='do-not-expose')})))

    def add(self, c, kind, value):
        c.execute('INSERT INTO records VALUES(?,?,?)',(kind,value['id'],json.dumps(value)))

    def write(self, relative, value):
        path=self.base/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(value if isinstance(value,str) else json.dumps(value))

    def test_snapshot_links_and_privacy(self):
        before=hashlib.sha256(self.db.read_bytes()).hexdigest()
        data,paths=Dashboard(self.root).snapshot()
        self.assertEqual(data['outbox'][0]['project'],'p')
        self.assertEqual(data['reports'][0]['task'],'t')
        self.assertNotIn('actions',data['reports'][0])
        self.assertNotIn('metrics',data['jobs'][0])
        self.assertEqual(data['checkpoints'][0]['next_step'],'Review')
        self.assertEqual(data['helpers'][0]['status'],'interrupted')
        self.assertNotIn('transcript',data['helpers'][0])
        self.assertNotIn('stdout',data['sessions'][0])
        self.assertNotIn('do-not-expose',json.dumps(data))
        self.assertEqual(len(paths),3)
        self.assertTrue(all(p.is_relative_to(self.base) for p in paths.values()))
        self.assertEqual(before,hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_metrics_are_loaded_only_as_objects(self):
        self.write('runs/j/report.json',dict(metrics={'reward':[1,2,3]},summaries={'reward':{'mean':2}}))
        data,_=Dashboard(self.root).snapshot()
        self.assertEqual(data['jobs'][0]['metrics']['reward'],[1,2,3])

    def test_missing_database_is_empty_without_creating_it(self):
        root=self.root/'empty'
        data,_=Dashboard(root).snapshot()
        self.assertEqual(data['projects'],[])
        self.assertTrue(data['warnings'])
        self.assertFalse(root.exists())

    def test_account_usage_is_read_only_and_allowlisted(self):
        (self.root/'config.yaml').write_text('claude:\n  accounts:\n    - id: primary\n    - id: secondary\nsubscription:\n  max_age_seconds: 900\n')
        now = time.time()
        with sqlite3.connect(self.db) as c:
            self.add(c, 'settings', dict(id='subscription:primary', observed_at=now-60,
                windows={'five_hour':dict(used_percentage=25, resets_at=now+3600, accessToken='do-not-expose'),
                         'seven_day_opus':dict(used_percentage=80, resets_at=now+86400),
                         'private':dict(used_percentage=1, token='do-not-expose')},
                credentials='do-not-expose'))
            self.add(c, 'settings', dict(id='account_logins', updated=now, logins=dict(primary=True,secondary=False), token='do-not-expose'))
            self.add(c, 'settings', dict(id='cooldown:primary', until=now+300, reason='do-not-expose'))
            self.add(c, 'sessions', dict(id='active', project='p', account='primary', status='running'))
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        data,_ = Dashboard(self.root).snapshot()
        primary, secondary = data['accounts']
        self.assertEqual(primary['status'], 'ok')
        self.assertEqual(primary['windows']['five_hour']['remaining_percentage'], 75)
        self.assertEqual(primary['active_sessions'], 1)
        self.assertTrue(primary['login_usable'])
        self.assertGreater(primary['cooldown_until'], now)
        self.assertEqual(secondary['status'], 'unavailable')
        self.assertFalse(secondary['login_usable'])
        self.assertNotIn('private', primary['windows'])
        self.assertNotIn('do-not-expose', json.dumps(data))
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_stale_account_usage_and_unknown_login(self):
        with sqlite3.connect(self.db) as c:
            self.add(c, 'settings', dict(id='subscription:a', observed_at=time.time()-1000,
                windows={'five_hour':dict(used_percentage=100, resets_at=time.time()-1)}, error='do-not-expose'))
            self.add(c, 'settings', dict(id='account_logins', updated=time.time()-1000, logins={'a':True}))
        data,_ = Dashboard(self.root).snapshot()
        account = data['accounts'][0]
        self.assertEqual(account['status'], 'stale')
        self.assertIsNone(account['login_usable'])
        self.assertTrue(account['metadata_error'])
        self.assertEqual(account['windows']['five_hour']['remaining_percentage'], 0)
        self.assertNotIn('do-not-expose', json.dumps(data))

    def test_server_telemetry_is_read_only_and_allowlisted(self):
        with sqlite3.connect(self.db) as c:
            self.add(c,'settings',dict(id='server_status',observed_at=time.time(),token='do-not-expose',_cpu_ticks=dict(total=10,idle=2),cpu=dict(threads=56,utilization_percent=25,secret='do-not-expose'),memory=dict(total_bytes=1000,available_bytes=400,used_bytes=600),gpus=[dict(index=0,name='NVIDIA A10',utilization_percent=50,lab_reserved=True,command='do-not-expose')]))
        before=hashlib.sha256(self.db.read_bytes()).hexdigest()
        data,_=Dashboard(self.root).snapshot()
        self.assertEqual(data['server']['status'],'ok')
        self.assertEqual(data['server']['cpu']['threads'],56)
        self.assertTrue(data['server']['gpus'][0]['lab_reserved'])
        self.assertNotIn('do-not-expose',json.dumps(data))
        self.assertEqual(before,hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_usage_refresh_requests_daemon_without_reading_credentials(self):
        dashboard=Dashboard(self.root)
        self.assertEqual(dashboard.request_usage_refresh()[0],503)
        (self.root/'runtime/daemon.json').write_text('{"status":"running"}')
        before=hashlib.sha256(self.db.read_bytes()).hexdigest()
        code,data=dashboard.request_usage_refresh()
        self.assertEqual(code,202)
        marker=self.root/'runtime/subscription-refresh.request'
        self.assertTrue(marker.exists())
        self.assertEqual(dashboard.request_usage_refresh()[1]['requested_at'],data['requested_at'])
        marker.unlink()
        self.assertEqual(dashboard.request_usage_refresh()[0],429)
        self.assertEqual(before,hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_http_is_read_only_and_restricts_paths_and_origins(self):
        server=make_server(self.root,0)
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
        self.addCleanup(conn.close)
        def request(path,method='GET',headers=None):
            conn.request(method,path,headers=headers or {})
            response=conn.getresponse()
            body=response.read()
            return response,body
        response,body=request('/api/snapshot')
        self.assertEqual(response.status,200)
        data=json.loads(body)
        fid=next(f['id'] for f in data['files'] if f['name']=='note.md')
        response,body=request('/api/file?id='+fid)
        self.assertEqual(body,b'# Note\nEvidence')
        self.assertTrue(response.getheader('Content-Type').startswith('text/plain'))
        for path in ['/.slack.env','/runtime/lab.sqlite3','/../config.yaml','/api/file?id=../../runtime/lab.sqlite3']:
            self.assertEqual(request(path)[0].status,404)
        self.assertEqual(request('/api/snapshot',headers={'Host':'evil.example'})[0].status,403)
        self.assertEqual(request('/api/snapshot',headers={'Origin':'https://evil.example'})[0].status,403)
        self.assertEqual(request('/api/snapshot','POST')[0].status,405)
        self.assertEqual(request('/api/subscription-refresh','POST')[0].status,503)
        self.assertEqual(request('/api/subscription-refresh','POST',headers={'Origin':'https://evil.example'})[0].status,403)
        (self.root/'runtime/daemon.json').write_text('{"status":"running"}')
        self.assertEqual(request('/api/subscription-refresh','POST')[0].status,202)
        self.assertEqual(request('/',headers={'Host':'127.0.0.1:63412'})[0].status,200)
        self.assertEqual(request('/api/snapshot',headers={'Host':'127.0.0.1:63412','Origin':'http://127.0.0.1:63412'})[0].status,200)
        self.assertEqual(request('/',headers={'Host':'127.0.0.1:63412','Origin':'http://localhost:63412'})[0].status,403)
        for host in ['127.0.0.1.evil.example:63412','localhost:99999','localhost:0','user@localhost:8765']:
            self.assertEqual(request('/',headers={'Host':host})[0].status,403)
        for path in ['/','/app.js','/help.js','/style.css']:
            response,body=request(path)
            self.assertEqual(response.status,200)
            self.assertTrue(body)
            self.assertIn("script-src 'self'",response.getheader('Content-Security-Policy'))
        response, plain = request('/app.js')
        self.assertIsNone(response.getheader('Content-Encoding'))
        response, compressed = request('/app.js', headers={'Accept-Encoding': 'br, gzip, deflate'})
        self.assertEqual(response.getheader('Content-Encoding'), 'gzip')
        self.assertEqual(response.getheader('Vary'), 'Accept-Encoding')
        self.assertEqual(int(response.getheader('Content-Length')), len(compressed))
        self.assertEqual(gzip.decompress(compressed), plain)
        self.assertLess(len(compressed), len(plain))
        response, body = request('/app.js', headers={'Accept-Encoding': 'gzip;q=0'})
        self.assertIsNone(response.getheader('Content-Encoding'))
        self.assertEqual(body, plain)
        response, body = request('/api/snapshot', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(response.getheader('Content-Encoding'), 'gzip')
        self.assertIn('projects', json.loads(gzip.decompress(body)))


if __name__=='__main__':
    unittest.main()
