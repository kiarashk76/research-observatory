"""Explicit CLI renewal stays separate from metadata polling."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from labcore.subscription import Monitor


class RenewalTests(unittest.TestCase):
    def monitor(self, root):
        engine = SimpleNamespace(root=Path(root), cfg={'root':root,'claude':{'bin':'claude','accounts':[
            {'id':'secondary','config_dir':'runtime/claude-auth/secondary/config'}]}})
        monitor = Monitor(engine)
        monitor.renewal_state = Mock()
        monitor._refresh = Mock()
        monitor.renewing.add('secondary')
        return monitor

    def test_success_uses_named_credentials_and_rechecks_usage(self):
        with tempfile.TemporaryDirectory() as root:
            monitor = self.monitor(root)
            with patch('labcore.subscription.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
                monitor.renew('secondary')
            args = run.call_args
            self.assertIn('--no-session-persistence',args.args[0])
            self.assertIn('--tools',args.args[0])
            self.assertTrue(args.kwargs['env']['CLAUDE_CONFIG_DIR'].endswith('/secondary/config'))
            self.assertEqual(args.kwargs['timeout'],90)
            monitor._refresh.assert_called_once_with(True,'secondary')
            self.assertNotIn('secondary',monitor.renewing)

    def test_failure_does_not_retry_or_fetch(self):
        with tempfile.TemporaryDirectory() as root:
            monitor = self.monitor(root)
            with patch('labcore.subscription.subprocess.run',return_value=SimpleNamespace(returncode=1)) as run:
                monitor.renew('secondary')
            self.assertEqual(run.call_count,1)
            monitor._refresh.assert_not_called()
            self.assertEqual(monitor.renewal_state.call_args.args[1],'failed')
