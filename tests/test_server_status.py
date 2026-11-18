"""Hardware snapshots use counters, retain privacy, and explain eligibility."""
import subprocess
import unittest
from unittest.mock import Mock, patch

from labcore import server_status


class ServerStatusTests(unittest.TestCase):
    def collect(self, gpu_error=False):
        policy=dict(allowed_gpus='all',reserve_cpu_threads=1,min_gpu_free_fraction=.9,max_gpu_utilization=10)
        gpu=Mock(stdout='0, uuid0, NVIDIA A10, 24000, 23000, 0, 40\n1, uuid1, NVIDIA A10, 24000, 23000, 0, 41\n2, uuid2, NVIDIA A10, 24000, 23000, 0, 42\n3, uuid3, NVIDIA A10, 24000, 1000, 90, 70\n')
        processes=Mock(stdout='uuid0\nuuid2\n')
        jobs=[dict(status='running',allocation=dict(cpus=[0],gpus=['uuid0']))]
        def read(path):
            if str(path)=='/proc/stat':return ''.join(f'cpu{i} 100 0 0 100 0 0 0 0 0 0\n' for i in range(4))
            return 'MemTotal: 1000 kB\nMemAvailable: 400 kB\n'
        with patch('labcore.server_status.os.sched_getaffinity',return_value=set(range(4))),patch('labcore.server_status.os.getloadavg',return_value=(1.,2.,3.)),patch('labcore.server_status.Path.read_text',autospec=True,side_effect=read),patch('labcore.server_status.subprocess.run',side_effect=FileNotFoundError() if gpu_error else [gpu,processes]):
            return server_status.collect(policy,jobs,dict(_cpu_ticks=dict(total=400,idle=200)))

    def test_cpu_memory_and_gpu_eligibility(self):
        state=self.collect()
        self.assertEqual(state['cpu']['utilization_percent'],50)
        self.assertEqual(state['cpu']['threads'],4)
        self.assertEqual(state['cpu']['lab_reserved_threads'],1)
        self.assertEqual(state['cpu']['schedulable_threads'],2)
        self.assertEqual(state['memory']['used_bytes'],600*1024)
        self.assertTrue(state['gpus'][0]['lab_reserved'])
        self.assertFalse(state['gpus'][0]['available_for_lab'])
        self.assertTrue(state['gpus'][1]['available_for_lab'])
        self.assertFalse(state['gpus'][2]['available_for_lab'])
        self.assertIn('Compute process present',state['gpus'][2]['unavailable_reasons'])
        self.assertIn('Below required free memory',state['gpus'][3]['unavailable_reasons'])

    def test_gpu_failure_preserves_cpu_and_memory(self):
        state=self.collect(gpu_error=True)
        self.assertTrue(state['gpu_error'])
        self.assertEqual(state['cpu']['threads'],4)
        self.assertTrue(state['memory'])

    def test_stale_and_unavailable_views_strip_internal_data(self):
        state=self.collect();state['token']='do-not-expose';state['cpu']['secret']='do-not-expose'
        result=server_status.view(state,now=state['observed_at']+60)
        self.assertEqual(result['status'],'stale')
        self.assertNotIn('_cpu_ticks',result)
        self.assertNotIn('secret',result['cpu'])
        self.assertNotIn('token',result)
        self.assertEqual(server_status.view({})['status'],'unavailable')
