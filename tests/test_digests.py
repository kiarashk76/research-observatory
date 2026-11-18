"""Digest publication and weekly scheduling; isolated records, no models/network."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from labcore.config import load
from labcore.engine import Engine
from labcore.dashboard import Dashboard
from labcore import wakeups


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = load()
        cfg['root'] = self.temp.name
        self.engine = Engine(cfg)
        self.store = self.engine.store
        channel = self.engine.enroll('C', 'reading')
        self.project = channel['project']
        with self.store.tx() as tx:
            project = tx.get('projects', self.project)
            project['human_started'] = True
            tx.put('projects', project)
        self.session = dict(id='session', project=self.project, channel='C', thread='T', role='supervisor')
        self.action = dict(type='publish_digest', key='continual-learning', digest_record=dict(
            topic='Continual learning', title='Memory and adaptation', period_start='2026-11-01',
            period_end='2026-11-07', summary='A quiet week; no qualifying new papers.',
            text='## Search coverage and caveats\nNo papers matched the stated search scope.'))

    def apply(self, action, session=None):
        with self.store.tx() as tx:
            self.engine._action(tx, session or self.session, None, action)

    def test_publication_revision_export_and_dashboard(self):
        self.apply(self.action)
        self.action['digest_record']['summary'] = 'Corrected coverage note.'
        self.apply(self.action)
        self.assertEqual(len(self.store.all('digests')), 1)
        self.assertEqual(len(self.store.all('digest_versions')), 2)
        digest = self.store.all('digests')[0]
        self.assertEqual(digest['version'], 2)
        self.engine.refresh_views(self.project)
        project = self.store.get('projects', self.project)
        path = Path(self.temp.name)/'projects'/project['folder']/'notes/digests/continual-learning/2026-11-07.md'
        self.assertIn('Corrected coverage note.', path.read_text())
        data, _ = Dashboard(Path(self.temp.name)).snapshot()
        self.assertEqual(data['digests'][0]['summary'], 'Corrected coverage note.')
        self.assertNotIn('session', data['digests'][0])

    def test_structured_topics_publish_to_snapshot_and_markdown(self):
        sections=[dict(topic='Transformers',papers=[dict(title='A paper',url='https://arxiv.org/abs/1234.56789',
                    summary='A brief summary.',details='**Limitations:** A small evaluation.',canonical_id='arxiv:1234.56789')]),
                  dict(topic='Continual learning',summary='No new papers this issue.',papers=[])]
        record=dict(self.action['digest_record'],sections=sections)
        self.apply(dict(self.action,digest_record=record))
        data,_=Dashboard(Path(self.temp.name)).snapshot()
        self.assertEqual(data['digests'][0]['sections'],sections)
        self.engine.refresh_views(self.project)
        project=self.store.get('projects',self.project)
        text=(Path(self.temp.name)/'projects'/project['folder']/'notes/digests/continual-learning/2026-11-07.md').read_text()
        for value in ('## Transformers','A paper','A brief summary.','**Limitations:**','No new papers this issue.'):
            self.assertIn(value,text)

    def test_structured_topics_reject_unsafe_links_and_missing_content(self):
        from labcore.digests import validate_sections
        paper=dict(title='Paper',url='https://example.org/paper',summary='Brief',details='Methods and caveats')
        for bad in (dict(paper,url='javascript:alert(1)'),dict(paper,url='https://user:password@example.org/paper'),
                    dict(paper,summary=''),dict(paper,details=42)):
            with self.subTest(paper=bad),self.assertRaises(ValueError):
                validate_sections([dict(topic='Transformers',papers=[bad])])
        for bad in (None,{},[dict(topic='',papers=[])],[dict(topic='Topic',papers='not a list')]):
            with self.subTest(sections=bad),self.assertRaises(ValueError):validate_sections(bad)

    def test_supervisor_only_and_invalid_metadata(self):
        with self.assertRaises(ValueError):
            self.apply(self.action, dict(self.session, role='literature'))
        for change in (dict(key='../escape'), dict(digest_record=dict(self.action['digest_record'], period_start='2026-12-01')),
                       dict(digest_record=dict(self.action['digest_record'], text=''))):
            with self.assertRaises(ValueError):
                self.apply(dict(self.action, **change))
        self.assertEqual(self.store.all('digests'), [])

    def test_transaction_rolls_back_publication(self):
        with self.assertRaises(ValueError):
            with self.store.tx() as tx:
                self.engine._action(tx, self.session, None, self.action)
                self.engine._action(tx, self.session, None, dict(type='not_authorized'))
        self.assertEqual(self.store.all('digest_versions'), [])

    def schedule(self):
        # Oct 31, 2025 is MDT; the following Friday is MST. Keep 09:00 local.
        now = dt.datetime.fromisoformat('2025-10-30T10:00:00-06:00').timestamp()
        with patch('labcore.wakeups.time.time', return_value=now):
            with self.store.tx() as tx:
                return wakeups.schedule(tx, self.session, dict(at='2025-10-31T09:00:00-06:00',
                    reason='Weekly digest: continual learning; up to five papers; publish in Library',
                    recurrence='weekly', timezone='America/Edmonton', key='digest-continual-learning'))

    def test_weekly_idempotency_dst_and_cancellation(self):
        wake = self.schedule()
        self.assertEqual(self.schedule()['id'], wake['id'])
        now = wake['at']+1
        wakeups.fire(self.engine, now)
        wakeups.fire(self.engine, now)
        requests = [r for r in self.store.all('requests') if r.get('payload', {}).get('wake') == wake['id']]
        self.assertEqual(len(requests), 1)
        saved = self.store.get('wakeups', wake['id'])
        self.assertEqual(saved['status'], 'pending')
        next_local = dt.datetime.fromtimestamp(saved['at'], wakeups.ZoneInfo('America/Edmonton'))
        self.assertEqual(next_local.isoformat(), '2025-11-07T09:00:00-07:00')
        # Even a week later, an unfinished review must not duplicate.
        wakeups.fire(self.engine, saved['at']+1)
        self.assertEqual(len([r for r in self.store.all('requests') if r.get('payload', {}).get('wake')]), 1)
        self.apply(dict(type='cancel_wake', wake_id=wake['id']))
        self.assertEqual(self.store.get('requests', requests[0]['id'])['status'], 'cancelled')
        wakeups.fire(self.engine, saved['at']+100)
        self.assertEqual(self.store.get('wakeups', wake['id'])['status'], 'cancelled')

    def test_pause_recovery_coalesces_missed_weeks_and_closure(self):
        wake = self.schedule()
        now = dt.datetime.fromisoformat('2025-11-21T12:00:00-07:00').timestamp()
        with self.store.tx() as tx:
            project = tx.get('projects', self.project)
            project['status'] = 'paused'
            tx.put('projects', project)
        wakeups.fire(self.engine, now)
        self.assertEqual(self.store.get('wakeups', wake['id'])['at'], wake['at'])
        with self.store.tx() as tx:
            project['status'] = 'active'
            tx.put('projects', project)
        # A new controller with the same durable store can fire it after restart.
        wakeups.fire(SimpleNamespace(store=self.store), now)
        saved = self.store.get('wakeups', wake['id'])
        self.assertGreater(saved['at'], now)
        self.assertEqual(len([r for r in self.store.all('requests') if r.get('payload', {}).get('wake')]), 1)
        with self.store.tx() as tx:
            tx.put('closed_threads', dict(id='C:T'))
        wakeups.fire(self.engine, saved['at']+1)
        self.assertEqual(self.store.get('wakeups', wake['id'])['status'], 'cancelled')

    def test_one_shot_unchanged(self):
        with self.store.tx() as tx:
            wake = wakeups.schedule(tx, self.session, dict(delay_minutes=5, reason='One review'))
        wakeups.fire(self.engine, wake['at']+1)
        self.assertEqual(self.store.get('wakeups', wake['id'])['status'], 'fired')
        self.assertIsNotNone(self.store.get('requests', 'wake:'+wake['id']))

    def test_next_week_creates_one_new_request_after_previous_finishes(self):
        wake = self.schedule()
        wakeups.fire(self.engine, wake['at']+1)
        saved = self.store.get('wakeups', wake['id'])
        with self.store.tx() as tx:
            request = tx.get('requests', saved['last_request_id'])
            request['status'] = 'done'
            tx.put('requests', request)
        wakeups.fire(self.engine, saved['at']+1)
        wakeups.fire(self.engine, saved['at']+2)
        requests = [r for r in self.store.all('requests') if r.get('payload', {}).get('wake') == wake['id']]
        self.assertEqual(len(requests), 2)
        self.assertEqual(sum(r['status']=='queued' for r in requests), 1)

    def test_invalid_schedule_never_commits(self):
        for changes in (dict(recurrence='daily'), dict(timezone='not/a/timezone')):
            with self.assertRaises((ValueError, KeyError)):
                with self.store.tx() as tx:
                    wakeups.schedule(tx, self.session, dict(delay_minutes=5, reason='Reading', **changes))
        self.assertEqual(self.store.all('wakeups'), [])


if __name__ == '__main__':
    unittest.main()
