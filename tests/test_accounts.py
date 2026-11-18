"""Multi-account routing before model launches, with isolated authentication."""
import json
import time
import unittest
from unittest.mock import Mock, patch

import test_subscription as fixtures
from labcore import accounts, auth, subscription


class AccountTests(unittest.TestCase):
    cleanup_processes = fixtures.SubscriptionTests.cleanup_processes
    session = fixtures.SubscriptionTests.session
    policy = fixtures.SubscriptionTests.policy
    task = fixtures.SubscriptionTests.task

    def setUp(self):
        fixtures.SubscriptionTests.setUp(self)
        self.policy()
        self.cfg['claude']['accounts'] = [dict(id=name, config_dir='runtime/claude-auth/' + name + '/config') for name in ('a', 'b')]
        for name, usage in [('a', 90), ('b', 20)]:
            directory = accounts.directory(self.cfg, name)
            directory.mkdir(parents=True)
            (directory / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': 'fake-' + name, 'expiresAt': (time.time() + 3600) * 1000}}))
            self.usage(name, usage)

    def usage(self, name, used, **extra):
        with self.engine.store.tx() as tx:
            tx.put('settings', dict(id='subscription:' + name, observed_at=time.time(), attempted_at=time.time(), windows=subscription.normalize(dict(five_hour=dict(utilization=used, resets_at=time.time() + 3600), **extra))))

    def test_selects_before_supervisor_launch(self):
        with patch.object(self.runtime.runner, 'start') as launch:
            self.runtime.dispatch()
        self.assertEqual(launch.call_args.args[0]['account'], 'b')
        self.assertIn('accounts', self.engine.context(launch.call_args.args[0])['subscription'])

    def test_model_specific_and_all_exhausted(self):
        self.usage('b', 20, seven_day_opus=dict(utilization=100, resets_at=time.time() + 3600))
        self.assertEqual(accounts.choose(self.engine, 'opus', 'supervisor')[0], 'a')
        self.usage('a', 100)
        self.assertIsNotNone(subscription.gate(self.engine, 'opus', 'supervisor'))
        self.assertIsNone(subscription.gate(self.engine, 'sonnet', 'supervisor'))

    def test_cooldown_and_missing_login_only_exclude_one_account(self):
        with self.engine.store.tx() as tx:
            tx.put('settings', dict(id='cooldown:b', until=time.time() + 300))
        self.assertEqual(accounts.choose(self.engine, 'opus', 'supervisor')[0], 'a')
        (accounts.directory(self.cfg, 'a') / '.credentials.json').unlink()
        with patch.object(self.runtime.runner, 'start') as launch:
            self.runtime.dispatch()
        launch.assert_not_called()

    def test_auth_failure_and_recovery_are_per_account(self):
        session = self.session()
        session['account'] = 'b'
        self.runtime.runner.fail(session, 'expired', auth_required=True)
        self.assertTrue(self.engine.store.get('settings', 'authentication:b')['required'])
        self.assertEqual(accounts.choose(self.engine, 'opus', 'supervisor')[0], 'a')
        with patch('labcore.auth.source_state', return_value=dict(usable=True, mtime_ns=1)):
            auth.recover(self.engine)
        self.assertFalse(self.engine.store.get('settings', 'authentication:b')['required'])

    def test_environment_keeps_account_credentials_separate(self):
        a = self.runtime.runner.environment(self.root / 'workspaces/a', 'a')
        b = self.runtime.runner.environment(self.root / 'workspaces/b', 'b')
        self.assertNotEqual(a['CLAUDE_CONFIG_DIR'], b['CLAUDE_CONFIG_DIR'])
        self.assertIn('fake-a', (accounts.directory(self.cfg, 'a') / '.credentials.json').read_text())

    def test_metadata_polling_is_independent(self):
        fetcher = Mock(side_effect=[OSError('offline'), {'five_hour': {'utilization': 45}}])
        subscription.Monitor(self.engine, fetcher).refresh(force=True)
        self.assertEqual(fetcher.call_count, 2)
        self.assertIsNotNone(self.engine.store.get('settings', 'subscription:a')['error'])
        self.assertEqual(subscription.view(self.engine, account='b')['windows']['five_hour']['used_percentage'], 45)

    def test_specialists_choose_independently_and_session_stays_pinned(self):
        for request in self.engine.store.all('requests'):
            with self.engine.store.tx() as tx:
                request['status'] = 'done'
                tx.put('requests', request)
        self.task()
        with patch.object(self.runtime.runner, 'start') as launch:
            self.runtime.dispatch()
        session = launch.call_args.args[0]
        self.assertEqual(session['account'], 'b')
        self.usage('a', 0)
        self.usage('b', 100)
        self.assertEqual(accounts.choose(self.engine, 'opus', 'supervisor')[0], 'a')
        self.assertEqual(self.engine.store.get('sessions', session['id'])['account'], 'b')

    def test_stale_readings_do_not_claim_free_capacity(self):
        with self.engine.store.tx() as tx:
            state = tx.get('settings', 'subscription:b')
            state['observed_at'] -= 1000
            tx.put('settings', state)
        self.assertEqual(accounts.choose(self.engine, 'opus', 'supervisor')[0], 'a')

    def test_new_login_retries_cached_credential_error_immediately(self):
        with self.engine.store.tx() as tx:
            state = tx.get('settings', 'subscription:a')
            state.update(error='Claude login needs refreshing; metadata is unavailable until normal CLI authentication refreshes', retry_at=time.time()+300)
            tx.put('settings', state)
        fetcher = Mock(return_value={'five_hour': {'utilization': 35}})
        subscription.Monitor(self.engine, fetcher).refresh(force=True)
        self.assertIsNone(self.engine.store.get('settings', 'subscription:a')['error'])
        self.assertEqual(subscription.view(self.engine, account='a')['windows']['five_hour']['used_percentage'], 35)

    def test_credential_change_does_not_bypass_provider_rate_limit(self):
        with self.engine.store.tx() as tx:
            state = tx.get('settings', 'subscription:a')
            state.update(error='Account metadata HTTP 429', error_kind='provider', source_mtime_ns=0, retry_at=time.time()+300)
            tx.put('settings', state)
        fetcher = Mock(return_value={'five_hour': {'utilization': 35}})
        subscription.Monitor(self.engine, fetcher).refresh(force=True)
        self.assertEqual(fetcher.call_count, 1)  # Only the other account is refreshed.
        self.assertEqual(fetcher.call_args.kwargs['account'], 'b')
