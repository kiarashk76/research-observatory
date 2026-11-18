"""Slack progress reactions follow real request/session state without model calls."""
import time
import unittest
from unittest.mock import patch

from slack_sdk.errors import SlackApiError
import test_platform as fixtures
from labcore.activity import Activity


class ActivityTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    cleanup_processes = fixtures.PlatformTests.cleanup_processes
    session = fixtures.PlatformTests.session

    def request(self, status, session=None):
        with self.engine.store.tx() as tx:
            request = tx.get('requests', 'C1:100.1')
            request.update(status=status)
            if session:
                request['session'] = session['id']
            tx.put('requests', request)

    def test_received_thinking_finished_and_idle_do_not_duplicate_reactions(self):
        with patch.object(self.runtime.runner, 'start') as model:
            self.bridge.activity.sync()
            self.bridge.activity.sync()
            self.assertEqual([r['name'] for r in self.client.reactions], ['eyes'])
            session = self.session()
            self.request('running', session)
            self.bridge.activity.sync()
            self.assertEqual([(r['action'], r['name']) for r in self.client.reactions],
                             [('add', 'eyes'), ('remove', 'eyes'), ('add', 'thinking_face')])
            self.request('done')
            self.bridge.activity.sync()
            self.bridge.activity.sync()
            self.assertEqual(self.client.reactions[-1]['action'], 'remove')
            self.assertEqual(len(self.client.reactions), 4)
            self.assertTrue(all(r['timestamp'] == '100.1' for r in self.client.reactions))
            model.assert_not_called()

    def test_thinking_clears_after_failure_cancel_or_close_and_across_restart(self):
        session = self.session()
        self.request('running', session)
        self.bridge.activity.sync()
        for status in ('blocked', 'cancelled', 'done'):
            self.request(status)
            Activity(self.engine, self.client).sync()
            self.assertEqual(self.engine.store.get('activity', 'C1:100.1')['reaction'], '')
        self.request('running', session)
        self.bridge.activity.sync()
        with self.engine.store.tx() as tx:
            tx.put('closed_threads', dict(id='C1:100.1'))
        Activity(self.engine, self.client).sync()
        self.assertEqual(self.client.reactions[-1]['name'], 'thinking_face')
        self.assertEqual(self.client.reactions[-1]['action'], 'remove')

    def test_queued_quota_wait_is_acknowledged_without_thinking(self):
        with self.engine.store.tx() as tx:
            tx.put('settings', dict(id='cooldown', until=time.time()+3600))
        self.bridge.activity.sync()
        self.assertEqual(self.client.reactions, [dict(action='add', channel='C1', timestamp='100.1', name='eyes')])

    def test_missing_scope_is_backed_off_without_losing_request(self):
        with patch.object(self.client, 'reactions_add', side_effect=SlackApiError('scope missing', {'error': 'missing_scope'})) as add:
            self.bridge.activity.sync()
            self.bridge.activity.sync()
            add.assert_called_once()
        self.assertEqual(self.engine.store.get('settings', 'slack_activity')['error'], 'missing_scope')
        self.assertEqual(self.engine.store.get('requests', 'C1:100.1')['status'], 'queued')

    def test_duplicate_and_missing_reaction_errors_are_idempotent(self):
        with patch.object(self.client, 'reactions_add', side_effect=SlackApiError('exists', {'error': 'already_reacted'})):
            self.bridge.activity.sync()
        self.assertEqual(self.engine.store.get('activity', 'C1:100.1')['reaction'], 'eyes')
        self.request('done')
        with patch.object(self.client, 'reactions_remove', side_effect=SlackApiError('gone', {'error': 'no_reaction'})):
            Activity(self.engine, self.client).sync()
        self.assertEqual(self.engine.store.get('activity', 'C1:100.1')['reaction'], '')

    def test_automated_review_does_not_react_on_an_invented_message(self):
        self.request('done')
        self.engine.store.enqueue(self.channel['project'], '100.1', 'Scheduled review',
                                  key='wake:test', payload={'channel': 'C1'})
        self.bridge.activity.sync()
        self.assertFalse(self.client.reactions)

    def test_reply_targets_its_own_message_in_thread(self):
        self.request('done')
        self.bridge.message(dict(channel='C1', ts='100.2', thread_ts='100.1', user='human', text='Follow-up question'))
        self.assertEqual(self.client.reactions[-1]['timestamp'], '100.2')
