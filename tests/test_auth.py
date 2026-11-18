"""Expired CLI login pauses launches and resumes only after credentials change."""
import json
import unittest
from unittest.mock import patch

import test_platform as fixtures
from labcore import auth


class AuthTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    cleanup_processes = fixtures.PlatformTests.cleanup_processes
    session = fixtures.PlatformTests.session
    task = fixtures.PlatformTests.task

    def fail_login(self):
        session = self.session()
        session['request_ids'] = ['C1:100.1']
        with self.engine.store.tx() as tx:
            tx.put('sessions', session)
        with patch('labcore.auth.source_state', return_value=dict(mtime_ns=100, usable=False)):
            self.runtime.runner.fail(session, 'Login expired', auth_required=True)
        return session

    def test_auth_failure_retains_request_and_does_not_launch_repeatedly(self):
        session = self.fail_login()
        self.assertEqual(self.engine.store.get('requests', 'C1:100.1')['status'], 'queued')
        self.assertEqual(self.engine.store.get('requests', 'C1:100.1').get('attempts'), 0)
        self.assertEqual(self.engine.store.get('sessions', session['id'])['status'], 'auth')
        with patch.object(self.runtime.runner, 'start') as launch:
            self.runtime.dispatch()
            self.runtime.dispatch()
            launch.assert_not_called()
        notices = self.engine.store.all('outbox')
        self.assertEqual(len(notices), 1)
        self.assertIn('claude auth login', notices[0]['text'])
        auth.notice(self.engine, 'C1', '100.1')
        self.assertEqual(len(self.engine.store.all('outbox')), 1)

    def test_only_changed_usable_login_clears_hold(self):
        self.fail_login()
        for state in (dict(mtime_ns=100, usable=True), dict(mtime_ns=101, usable=False)):
            with patch('labcore.auth.source_state', return_value=state):
                self.runtime.recover_auth()
            self.assertTrue(self.engine.store.get('settings', 'authentication')['required'])
        with patch('labcore.auth.source_state', return_value=dict(mtime_ns=101, usable=True)):
            self.runtime.recover_auth()
        self.assertFalse(self.engine.store.get('settings', 'authentication')['required'])
        with patch.object(self.runtime.runner, 'start') as launch:
            self.runtime.dispatch()
            launch.assert_called_once()

    def test_source_checks_actual_login_instead_of_file_presence(self):
        with patch('labcore.auth.Path.home', return_value=self.root):
            path = self.root/'.claude/.credentials.json'
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(dict(claudeAiOauth=dict(accessToken='', refreshToken='', expiresAt=0))))
            self.assertFalse(auth.source_state()['usable'])
            path.write_text(json.dumps(dict(claudeAiOauth=dict(accessToken='fake', refreshToken='fake', expiresAt=1))))
            self.assertTrue(auth.source_state()['usable'])

    def test_cli_auth_error_is_classified_before_generic_failure(self):
        session = self.session()
        out = self.root/'auth-output.json'
        err = self.root/'auth-stderr.txt'
        out.write_text(json.dumps(dict(type='result', is_error=True,
            result='Failed to authenticate: OAuth session expired and could not be refreshed', usage={})))
        err.write_text('')
        session.update(pid=999999999, identity='gone', stdout=str(out), stderr=str(err), request_ids=['C1:100.1'])
        with self.engine.store.tx() as tx:
            tx.put('sessions', session)
        with patch('labcore.auth.source_state', return_value=dict(mtime_ns=100, usable=False)):
            self.runtime.runner.poll()
        self.assertEqual(self.engine.store.get('sessions', session['id'])['status'], 'auth')
        self.assertTrue(self.engine.store.get('settings', 'authentication')['required'])
        self.assertEqual(self.engine.store.get('requests', 'C1:100.1')['status'], 'queued')

    def test_zero_usage_auth_error_does_not_exhaust_specialist_session_count(self):
        task = self.task(budget=dict(minutes=2, sessions=1, children=0))
        session = self.session('engineer', task)
        with patch('labcore.auth.source_state', return_value=dict(mtime_ns=100, usable=False)):
            self.runtime.runner.fail(session, 'Login expired', auth_required=True)
        with self.engine.store.tx() as tx:
            project = tx.get('projects', task['project'])
            self.assertIsNone(self.engine.budget_available(tx, project, .1, task['root_task']))
