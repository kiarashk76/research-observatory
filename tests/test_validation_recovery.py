"""Invalid assignments must give actionable feedback and never disappear silently."""
import unittest
from unittest.mock import patch

import test_platform as fixtures


class ValidationRecoveryTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    cleanup_processes = fixtures.PlatformTests.cleanup_processes
    session = fixtures.PlatformTests.session

    def test_context_exposes_session_ceiling_and_creation_rejects_excess(self):
        session=self.session()
        self.assertEqual(self.engine.context(session)['assignment_limits']['max_session_minutes'],self.cfg['scheduling']['max_session_minutes'])
        with self.engine.store.tx() as tx:
            with self.assertRaisesRegex(ValueError,'session_minutes=.*at most'):
                self.engine.create_task(tx,session,dict(role='engineer',goal='Work',acceptance='Evidence',model='sonnet',effort='medium',session_minutes=self.cfg['scheduling']['max_session_minutes']+1))

    def test_retry_receives_rejection_and_exhaustion_posts_notice(self):
        self.cfg["scheduling"]["max_error_retries"] = 1
        with patch.object(self.runtime.runner,'start') as launch:
            self.runtime.dispatch()
        session=launch.call_args.args[0]
        self.runtime.runner.fail(session,'Requested session_minutes=45; at most 30',retry=True)
        with patch.object(self.runtime.runner,'start') as launch:
            self.runtime.dispatch()
        retry=launch.call_args.args[0]
        self.assertTrue(any('at most 30' in reason for reason in retry['reasons']))
        self.runtime.runner.fail(retry,'Requested session_minutes=45; at most 30',retry=True)
        self.assertEqual(self.engine.store.get('requests','C1:100.1')['status'],'blocked')
        self.assertEqual(len(self.engine.store.all('outbox')),1)

    def test_expanded_config_accepts_long_assignments_and_keeps_ceiling(self):
        session=self.session()
        with self.engine.store.tx() as tx:
            task=self.engine.create_task(tx,session,dict(role='engineer',key='expanded-capacity',goal='Long implementation',acceptance='Evidence',model='sonnet',effort='high',session_minutes=120,budget=dict(minutes=720,sessions=48,children=16,output_tokens=1000000)))
            self.assertEqual(task['session_minutes'],120)
            self.assertEqual(task['budget']['sessions'],48)
            self.engine._action(tx,session,None,dict(type='grant',task=task['id'],budget=dict(sessions=64,children=32)))
            self.assertEqual(tx.get('tasks',task['id'])['budget']['sessions'],64)
            with self.assertRaisesRegex(ValueError,'exceeds configured'):
                self.engine.create_task(tx,session,dict(role='engineer',goal='Too many sessions',acceptance='Evidence',model='sonnet',effort='high',session_minutes=120,budget=dict(minutes=720,sessions=self.cfg['scheduling']['max_task_sessions']+1)))

    def test_supervisor_grant_and_answer_preserve_running_task(self):
        task=fixtures.PlatformTests.task(self)
        specialist=self.session('engineer',task)
        supervisor=self.session()
        for action in [dict(type='answer',task=task['id'],text='Continue the agreed work'),dict(type='grant',task=task['id'],budget=dict(minutes=10))]:
            with self.engine.store.tx() as tx:
                self.engine._action(tx,supervisor,None,action)
            self.assertEqual(self.engine.store.get('tasks',task['id'])['status'],'running')

    def test_reconcile_repairs_queued_task_with_active_session(self):
        task=fixtures.PlatformTests.task(self)
        self.session('engineer',task)
        self.runtime.reconcile()
        self.assertEqual(self.engine.store.get('tasks',task['id'])['status'],'running')
        with patch.object(self.runtime.runner,'start') as launch:
            self.runtime.dispatch()
        self.assertFalse(any(call.args[0].get('task')==task['id'] for call in launch.call_args_list))
