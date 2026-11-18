"""Account metadata and durable wake-ups; all providers and model launches mocked."""
import datetime as dt
import time
import urllib.error
import unittest
from unittest.mock import Mock, patch

import test_platform as fixtures
from labcore import subscription, wakeups
from labcore.runtime import Runtime


class SubscriptionTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    cleanup_processes = fixtures.PlatformTests.cleanup_processes
    task = fixtures.PlatformTests.task
    session = fixtures.PlatformTests.session

    def policy(self):
        self.cfg['subscription'] = {'enabled': True, 'poll_seconds': 300, 'max_age_seconds': 900,
                                   'large_task_threshold_percent': 85, 'small_task_threshold_percent': 95}

    def snapshot(self, **windows):
        with self.engine.store.tx() as tx:
            tx.put('settings', {'id': 'subscription', 'observed_at': time.time(), 'attempted_at': time.time(),
                               'windows': subscription.normalize(windows)})

    def done_requests(self):
        with self.engine.store.tx() as tx:
            for request in tx.all('requests'):
                request['status'] = 'done'
                tx.put('requests', request)

    def test_utilization_is_already_percent_not_fraction(self):
        windows = subscription.normalize({'five_hour': {'utilization': .5, 'resets_at': None}, 'seven_day': None})
        self.assertEqual(windows['five_hour']['used_percentage'], .5)
        self.assertEqual(windows['five_hour']['remaining_percentage'], 99.5)
        self.assertNotIn('seven_day', windows)

    def test_metadata_cached_without_model_calls(self):
        self.policy()
        fetch = Mock(return_value={'five_hour': {'utilization': 20, 'resets_at': time.time() + 3600}})
        monitor = subscription.Monitor(self.engine, fetch)
        monitor.refresh(force=True)
        for _ in range(5):
            monitor.refresh()
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(self.engine.store.all('sessions'))
        data = self.engine.context(self.session())['subscription']
        self.assertEqual(data['windows']['five_hour']['remaining_percentage'], 80)
        parsed=dt.datetime.fromisoformat(data['windows']['five_hour']['resets_at_local'])
        from zoneinfo import ZoneInfo
        self.assertEqual(parsed.utcoffset(),dt.datetime.fromtimestamp(time.time()+3600,ZoneInfo(self.cfg['timezone'])).utcoffset())

    def test_idle_lab_does_not_poll_but_can_opt_in(self):
        self.policy()
        self.done_requests()
        fetch=Mock(return_value={'five_hour': {'utilization': 25}})
        monitor=subscription.Monitor(self.engine,fetch)
        monitor.refresh()
        fetch.assert_not_called()
        self.cfg['subscription']['poll_when_idle']=True
        monitor.refresh()
        fetch.assert_called_once()
        self.assertFalse(self.engine.store.all('sessions'))

    def test_dashboard_refresh_signal_is_consumed_without_model_calls(self):
        self.policy()
        self.snapshot(five_hour={'utilization': 20})
        marker=self.root/'runtime/subscription-refresh.request'
        marker.touch()
        stamp=marker.stat().st_mtime
        fetch=Mock(return_value={'five_hour': {'utilization': 30}})
        subscription.Monitor(self.engine,fetch).refresh()
        fetch.assert_called_once()
        self.assertFalse(marker.exists())
        self.assertEqual(self.engine.store.get('settings','subscription_refresh')['requested_at'],stamp)
        self.assertEqual(subscription.view(self.engine)['windows']['five_hour']['used_percentage'],30)
        self.assertFalse(self.engine.store.all('sessions'))

    def test_metadata_rate_limit_honors_backoff_even_for_manual_refresh(self):
        self.policy()
        error = urllib.error.HTTPError(subscription.ENDPOINT, 429, 'limited', {'Retry-After': '1800'}, None)
        fetch = Mock(side_effect=error)
        monitor = subscription.Monitor(self.engine, fetch)
        monitor.refresh(force=True)
        monitor.refresh(force=True)
        self.assertEqual(fetch.call_count, 1)
        self.assertGreater(self.engine.store.get('settings', 'subscription')['retry_at'], time.time() + 1700)

    def test_failed_refresh_keeps_prior_snapshot_and_marks_stale(self):
        self.policy()
        self.snapshot(five_hour={'utilization': 100, 'resets_at': time.time() + 3600})
        with self.engine.store.tx() as tx:
            old = tx.get('settings', 'subscription')
            old['observed_at'] -= 1000
            old['attempted_at'] = 0
            tx.put('settings', old)
        subscription.Monitor(self.engine, Mock(side_effect=OSError('offline'))).refresh(force=True)
        self.assertEqual(subscription.view(self.engine)['status'], 'stale')
        self.assertIsNone(subscription.gate(self.engine, 'opus', 'large'))
        self.assertEqual(subscription.view(self.engine)['windows']['five_hour']['used_percentage'], 100)

    def test_large_work_waits_but_small_work_can_start(self):
        self.policy()
        self.snapshot(five_hour={'utilization': 90, 'resets_at': time.time() + 3600})
        self.done_requests()
        large = self.task(key='large', usage_class='large')
        small = self.task(key='small', role='literature', goal='Extract one definition', usage_class='small')
        with patch.object(self.runtime.runner, 'start') as launch:
            self.runtime.dispatch()
        self.assertEqual(self.engine.store.get('tasks', large['id'])['status'], 'queued')
        self.assertIn('subscription_hold', self.engine.store.get('tasks', large['id']))
        self.assertEqual(self.engine.store.get('tasks', small['id'])['status'], 'running')
        self.assertEqual(launch.call_count, 1)

    def test_small_work_holds_at_95_but_supervisor_can_answer(self):
        self.policy()
        self.snapshot(five_hour={'utilization': 96, 'resets_at': time.time() + 3600})
        task = self.task(usage_class='small')
        with patch.object(self.runtime.runner, 'start') as launch:
            self.runtime.dispatch()
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(launch.call_args.args[0]['role'], 'supervisor')
        self.assertEqual(self.engine.store.get('tasks', task['id'])['status'], 'queued')

    def test_quota_held_urgent_task_does_not_preempt_useful_small_work(self):
        self.policy()
        self.snapshot(five_hour={'utilization': 90, 'resets_at': time.time() + 3600})
        self.done_requests()
        small = self.task(key='current-small', usage_class='small', priority=50)
        self.task(key='urgent-large', usage_class='large', priority=95)
        running = self.session('engineer', small)
        self.runtime.preempt()
        self.assertFalse(self.engine.store.get('sessions', running['id']).get('preempted', False))

    def test_exhaustion_blocks_model_calls_and_notifies_once(self):
        self.policy()
        self.snapshot(five_hour={'utilization': 100, 'resets_at': time.time() + 3600})
        with patch.object(self.runtime.runner, 'start') as launch:
            self.runtime.dispatch()
            self.runtime.dispatch()
            launch.assert_not_called()
        self.assertEqual(len(self.engine.store.all('outbox')), 1)
        self.assertEqual(self.engine.store.get('requests', 'C1:100.1')['status'], 'queued')

    def test_model_specific_exhaustion_allows_configured_supervisor_fallback(self):
        self.policy()
        self.cfg['subscription']['supervisor_fallback_model'] = 'sonnet'
        self.snapshot(five_hour={'utilization': 20, 'resets_at': time.time() + 3600},
                      seven_day_opus={'utilization': 100, 'resets_at': time.time() + 86400})
        with patch.object(self.runtime.runner, 'start') as launch:
            self.runtime.dispatch()
        self.assertEqual(launch.call_args.args[0]['model'], 'sonnet')
        self.assertIn('model-specific quota', ' '.join(launch.call_args.args[0]['reasons']))

    def test_all_blocking_windows_must_reset(self):
        self.policy()
        now = time.time()
        self.snapshot(five_hour={'utilization': 90, 'resets_at': now + 3600},
                      seven_day={'utilization': 90, 'resets_at': now + 86400})
        hold = subscription.gate(self.engine, 'sonnet', 'large')
        self.assertAlmostEqual(hold['retry_at'], now + 86430)

    def test_passed_reset_does_not_invent_new_usage(self):
        self.policy()
        self.snapshot(five_hour={'utilization': 100, 'resets_at': time.time() - 1})
        self.assertIsNone(subscription.gate(self.engine, 'sonnet', 'large'))
        self.assertTrue(subscription.view(self.engine)['windows']['five_hour']['reset_passed'])
        self.assertEqual(subscription.view(self.engine)['windows']['five_hour']['used_percentage'], 100)

    def schedule(self):
        session = self.session()
        at = time.time() + 120
        with self.engine.store.tx() as tx:
            wake = wakeups.schedule(tx, session, {'delay_minutes': 2, 'key': 'reset-review', 'reason': 'Review the pending baseline after reset'})
        return wake

    def test_wake_fires_once_across_restarts_without_a_model_poll(self):
        wake = self.schedule()
        with patch.object(self.runtime.runner, 'start') as launch:
            wakeups.fire(self.engine, now=wake['at'] + 1)
            Runtime(self.engine).fire_wakes()
            wakeups.fire(self.engine, now=wake['at'] + 2)
            launch.assert_not_called()
        requests = [r for r in self.engine.store.all('requests') if r['id'] == 'wake:' + wake['id']]
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.engine.store.get('wakeups', wake['id'])['status'], 'fired')

    def test_wake_waits_while_project_is_paused_and_cancels_if_closed(self):
        wake = self.schedule()
        with self.engine.store.tx() as tx:
            project = tx.get('projects', wake['project'])
            project['status'] = 'paused'
            tx.put('projects', project)
        wakeups.fire(self.engine, now=wake['at'] + 1)
        self.assertEqual(self.engine.store.get('wakeups', wake['id'])['status'], 'pending')
        self.bridge.command(self.channel, '100.1', 'close this thread', '900.1')
        wakeups.fire(self.engine, now=wake['at'] + 2)
        self.assertEqual(self.engine.store.get('wakeups', wake['id'])['status'], 'cancelled')

    def test_reusing_pending_wake_key_updates_instead_of_duplicates(self):
        first = self.schedule()
        second = self.schedule()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.engine.store.all('wakeups')), 1)

    def test_deferred_task_stays_asleep_until_its_time(self):
        self.done_requests()
        task = self.task()
        session = self.session()
        with self.engine.store.tx() as tx:
            self.engine._action(tx, session, None, {'type': 'defer_task', 'task': task['id'], 'delay_minutes': 2})
        with patch.object(self.runtime.runner, 'start') as launch:
            self.runtime.dispatch()
            launch.assert_not_called()
        later = self.engine.store.get('tasks', task['id'])['not_before'] + 1
        with patch('labcore.runtime.time.time', return_value=later), patch.object(self.runtime.runner, 'start') as launch:
            self.runtime.reconcile()
            self.runtime.dispatch()
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(launch.call_args.args[0]['task'], task['id'])

    def test_specialist_cannot_schedule_its_own_wake(self):
        task = self.task()
        session = self.session('engineer', task)
        with self.engine.store.tx() as tx:
            with self.assertRaisesRegex(ValueError, 'not authorized'):
                self.engine._action(tx, session, task, {'type': 'schedule_wake', 'delay_minutes': 120, 'reason': 'More work'})

    def test_schedule_requires_timezone_and_concrete_reason(self):
        with self.assertRaises(ValueError):
            wakeups.when({'at': '2026-10-27T10:00:00'})
        session = self.session()
        with self.engine.store.tx() as tx:
            with self.assertRaises(ValueError):
                wakeups.schedule(tx, session, {'delay_minutes': 120, 'reason': ''})


if __name__ == '__main__':
    unittest.main()
