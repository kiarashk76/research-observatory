"""Shared GPU admission, physical-device grants and legacy exclusive behavior."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from labcore import resources, server_status
from labcore.config import load


class GPUSharingTests(unittest.TestCase):
    def setUp(self):
        self.policy = load()['resources']
        self.policy['gpu_sharing'].update(enabled=True, max_jobs_per_gpu=4, memory_headroom_mib=1024, memory_headroom_fraction=.1)

    def owner(self, **updates):
        value=dict(id='job',status='running',gpu_mode='shared',gpu_memory_mib=4096,
                   allocation=dict(gpus=['GPU-a'],cpus=[0]))
        value.update(updates)
        return value

    def capacity(self, jobs=(), total=24000, free=20000, utilization=50, processes=()):
        return resources.gpu_capacity(self.policy,jobs,'GPU-a',total,free,utilization,processes)

    def test_shared_memory_uses_both_reservations_and_live_memory_with_headroom(self):
        state=self.capacity([self.owner()])
        self.assertFalse(state['exclusive_available'])
        self.assertTrue(state['shared_available'])
        self.assertEqual(state['shared_available_mib'],17504)
        self.assertEqual(self.capacity([self.owner()],free=5000)['shared_available_mib'],2600)
        owners=[self.owner(id=str(i),gpu_memory_mib=7000) for i in range(3)]
        self.assertEqual(self.capacity(owners,free=20000)['shared_available_mib'],600)

    def test_exclusive_legacy_jobs_never_share(self):
        job=self.owner();job.pop('gpu_mode')
        self.assertFalse(self.capacity([job])['shared_available'])
        self.assertFalse(self.capacity([job])['exclusive_available'])

    def test_new_exclusive_jobs_have_no_shared_memory_reservation(self):
        state=self.capacity([self.owner(gpu_mode='exclusive',gpu_memory_mib=None)])
        self.assertFalse(state['shared_available'])
        self.assertEqual(state['reserved_memory_mib'],0)

    def test_invalid_saved_shared_reservation_blocks_admission(self):
        for memory in (None,0,-1,float('nan'),float('inf'),'4096',True):
            with self.subTest(memory=memory):
                state=self.capacity([self.owner(gpu_memory_mib=memory)])
                self.assertFalse(state['shared_available'])
                self.assertEqual(state['shared_available_mib'],0)

    def test_only_managed_shared_processes_are_admitted(self):
        with patch('labcore.resources.process_belongs_to_job',side_effect=lambda pid,j:pid=='10'):
            self.assertTrue(self.capacity([self.owner()],processes=['10'])['shared_available'])
            self.assertFalse(self.capacity([self.owner()],processes=['10','99'])['shared_available'])
        self.assertFalse(self.capacity(processes=['99'])['shared_available'])
        self.assertFalse(self.capacity([self.owner()],processes=[None])['shared_available'])

    def test_process_ownership_checks_real_ancestry_and_process_identity(self):
        from labcore.runner import identity
        job=dict(pid=os.getppid(),identity=identity(os.getppid()))
        self.assertTrue(resources.process_belongs_to_job(os.getpid(),job))
        self.assertFalse(resources.process_belongs_to_job(os.getpid(),dict(job,identity='stale')))
        self.assertFalse(resources.process_belongs_to_job(None,job))

    def test_invalid_capacity_is_rejected(self):
        for updates in (dict(total=0),dict(free=float('nan')),dict(utilization=float('inf'))):
            with self.assertRaises(ValueError):self.capacity(**updates)

    def test_job_slots_disabled_and_idle_rules(self):
        self.assertFalse(self.capacity([self.owner(id=str(i)) for i in range(4)])['shared_available'])
        self.assertFalse(self.capacity(utilization=80)['shared_available'])
        self.assertTrue(self.capacity(utilization=0)['shared_available'])
        self.policy['gpu_sharing']['enabled']=False
        self.assertFalse(self.capacity(utilization=0)['shared_available'])

    def test_allocation_packs_and_respects_distinct_physical_device_grant(self):
        free=dict(cpus=[1,2,3,4],gpus=['GPU-b'],gpu_details={
            'GPU-a':dict(shared_available=True,shared_available_mib=6000,shared_jobs=['job']),
            'GPU-b':dict(shared_available=True,shared_available_mib=20000,shared_jobs=[])})
        policy=dict(self.policy,assignment_gpu_ids=['GPU-a'],assignment_max_gpus=1)
        request=dict(gpus=1,cpu_threads=1,gpu_mode='shared',gpu_memory_mib=4096)
        with patch('labcore.resources.snapshot',return_value=free):
            self.assertEqual(resources.allocate(policy,[],request)['gpus'],['GPU-a'])
            self.assertIsNone(resources.allocate(policy,[],dict(request,gpu_memory_mib=7000)))
            self.assertIsNone(resources.allocate(policy,[],dict(request,gpu_mode='exclusive')))
            self.assertEqual(resources.allocate(self.policy,[],dict(request,gpu_mode='exclusive'))['gpus'],['GPU-b'])
            self.assertIsNone(resources.allocate(self.policy,[],dict(request,gpus=2,gpu_memory_mib=7000)))

    def test_gpu_probe_failure_fails_closed_but_cpu_work_can_run(self):
        with patch('labcore.resources.subprocess.run',side_effect=OSError('probe failed')):
            state=resources.snapshot(self.policy,[])
        self.assertEqual(state['gpu_details'],{})
        self.assertEqual(state['gpus'],[])
        self.assertTrue(state['gpu_error'])

    def fixture(self):
        from tests.test_platform import PlatformTests
        fixture=PlatformTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.cfg['resources']['gpu_sharing']['max_jobs_per_gpu']=4
        task=fixture.task(resources=dict(max_gpus=1,max_cpu_threads=8,max_jobs=4,total_job_hours=4))
        session=fixture.session('engineer',task)
        return fixture,session

    def test_requests_validate_memory_and_preserve_exclusive_default(self):
        fixture,session=self.fixture()
        base=dict(command=['true'],hours=.1,gpus=1,cpu_threads=1,gpu_mode='shared',gpu_memory_mib=4096)
        for i, updates in enumerate([dict(gpu_memory_mib=0),dict(gpu_memory_mib=float('inf')),dict(gpu_memory_mib=True),
                                      dict(gpu_memory_mib=None),dict(gpus=0),dict(gpu_mode='unknown'),dict(gpu_mode='exclusive')]):
            with self.assertRaises(ValueError):fixture.runtime.jobs.submit(session,dict(base,**updates),str(i))
        job=fixture.runtime.jobs.submit(session,base,'shared')
        self.assertEqual(job['gpu_memory_mib'],4096)
        legacy=fixture.runtime.jobs.submit(session,dict(command=['true'],hours=.1,gpus=1,cpu_threads=1),'legacy')
        self.assertEqual(legacy['gpu_mode'],'exclusive')
        fixture.cfg['resources']['gpu_sharing']['enabled']=False
        with self.assertRaisesRegex(ValueError,'disabled'):fixture.runtime.jobs.submit(session,base,'disabled')

    def test_two_jobs_in_one_assignment_can_share_its_one_gpu(self):
        fixture,session=self.fixture()
        base=dict(command=['true'],hours=.1,gpus=1,cpu_threads=1,gpu_mode='shared',gpu_memory_mib=4096)
        for i in range(2):fixture.runtime.jobs.submit(session,base,str(i))
        def launch(job,allocation):
            job.update(status='running',allocation=allocation)
        real_allocate=resources.allocate
        def probe(policy,jobs,request):
            with patch('labcore.resources.snapshot',return_value=dict(cpus=[1,2,3,4],gpus=['GPU-a'],gpu_details={
                    'GPU-a':dict(shared_available=True,shared_available_mib=16000,shared_jobs=[])})):
                return real_allocate(policy,jobs,request)
        with patch.object(fixture.runtime.jobs,'_launch',side_effect=launch),patch('labcore.jobs.resources.allocate',side_effect=probe):
            fixture.runtime.jobs.start_ready()
        jobs=fixture.engine.store.all('jobs')
        self.assertEqual([j['status'] for j in jobs],['running','running'])
        self.assertEqual([j['allocation']['gpus'] for j in jobs],[['GPU-a'],['GPU-a']])

    def test_shared_reservations_survive_restart_and_release_finished_slots(self):
        fixture,session=self.fixture()
        with fixture.engine.store.tx() as tx:
            task=tx.get('tasks',session['task']);task['resources']['max_jobs']=6;tx.put('tasks',task)
        request=dict(command=['true'],hours=.1,gpus=1,cpu_threads=1,gpu_mode='shared',gpu_memory_mib=4096)
        for i in range(5):fixture.runtime.jobs.submit(session,request,str(i))
        def launch(job,allocation):job.update(status='running',allocation=allocation)
        def probe(command,**kwargs):
            return SimpleNamespace(stdout='0, GPU-a, 24000, 24000, 0\n' if '--query-gpu=' in command[1] else '')
        with patch('labcore.resources.subprocess.run',side_effect=probe),patch('labcore.resources.os.sched_getaffinity',return_value=set(range(16))),patch('labcore.resources.os.getloadavg',return_value=(0,0,0)):
            with patch.object(fixture.runtime.jobs,'_launch',side_effect=launch):fixture.runtime.jobs.start_ready()
            self.assertEqual([j['status'] for j in fixture.engine.store.all('jobs')],['running']*4+['queued'])
            from labcore.engine import Engine
            from labcore.runtime import Runtime
            restarted=Runtime(Engine(fixture.cfg))
            with patch.object(restarted.jobs,'_launch') as run:
                restarted.jobs.start_ready();run.assert_not_called()
            with fixture.engine.store.tx() as tx:
                job=tx.all('jobs')[0];job['status']='done';tx.put('jobs',job)
            with patch.object(restarted.jobs,'_launch',side_effect=launch):restarted.jobs.start_ready()
            self.assertEqual([j['status'] for j in fixture.engine.store.all('jobs')],['done']+['running']*4)

    def test_multiple_launches_cannot_overbook_memory(self):
        fixture,session=self.fixture()
        request=dict(command=['true'],hours=.1,gpus=1,cpu_threads=1,gpu_mode='shared',gpu_memory_mib=10000)
        for i in range(3):fixture.runtime.jobs.submit(session,request,str(i))
        def launch(job,allocation):job.update(status='running',allocation=allocation)
        def probe(command,**kwargs):
            return SimpleNamespace(stdout='0, GPU-a, 24000, 24000, 0\n' if '--query-gpu=' in command[1] else '')
        with patch('labcore.resources.subprocess.run',side_effect=probe),patch('labcore.resources.os.sched_getaffinity',return_value=set(range(16))),patch('labcore.resources.os.getloadavg',return_value=(0,0,0)),patch.object(fixture.runtime.jobs,'_launch',side_effect=launch):
            fixture.runtime.jobs.start_ready()
        self.assertEqual([j['status'] for j in fixture.engine.store.all('jobs')],['running','running','queued'])

    def test_fractional_and_nonfinite_device_requests_are_rejected(self):
        fixture,session=self.fixture()
        base=dict(command=['true'],hours=.1,gpus=1,cpu_threads=1)
        for field in ('gpus','cpu_threads'):
            for value in (True,1.5,float('nan'),float('inf'),[],{}):
                with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                    fixture.runtime.jobs.submit(session,dict(base,**{field:value}),str((field,value)))

    def test_partial_bad_telemetry_does_not_offer_a_gpu(self):
        gpu=SimpleNamespace(stdout='0, GPU-a, A10, 24000, 24000, 0, 40\n1, GPU-b, A10, 0, 0, 0, 40\n')
        with patch('labcore.server_status.subprocess.run',side_effect=[gpu,SimpleNamespace(stdout='')]):
            state=server_status.collect(self.policy,[])
        self.assertTrue(state['gpu_error'])
        self.assertEqual(state['gpus'],[])

    def test_sixteen_shared_jobs_fill_four_devices_with_separate_cpu_caps(self):
        fixture,_=self.fixture()
        fixture.cfg['budgets'].update(task_jobs=16,max_jobs=16)  # Isolate from operator defaults.
        fixture.cfg['resources'].update(max_cpu_threads_per_assignment=40,max_cpu_threads_per_job=32)
        task=fixture.task(key='sixteen',resources=dict(max_gpus=4,total_job_hours=4))
        self.assertEqual(task['resources']['max_jobs'],16)
        self.assertEqual(task['resources']['max_cpu_threads'],fixture.cfg['resources']['max_cpu_threads_per_assignment'])
        session=fixture.session('engineer',task)
        request=dict(command=['true'],hours=.1,gpus=1,cpu_threads=2,gpu_mode='shared',gpu_memory_mib=1024)
        for i in range(16):fixture.runtime.jobs.submit(session,request,'sixteen-'+str(i))
        def launch(job,allocation):job.update(status='running',allocation=allocation)
        def probe(command,**kwargs):
            return SimpleNamespace(stdout=''.join(f'{i}, GPU-{i}, 24000, 24000, 0\n' for i in range(4)) if '--query-gpu=' in command[1] else '')
        with patch('labcore.resources.subprocess.run',side_effect=probe),patch('labcore.resources.os.sched_getaffinity',return_value=set(range(64))),patch('labcore.resources.os.getloadavg',return_value=(0,0,0)),patch.object(fixture.runtime.jobs,'_launch',side_effect=launch):
            fixture.runtime.jobs.start_ready()
        jobs=[j for j in fixture.engine.store.all('jobs') if j['root_task']==task['id']]
        self.assertEqual(len(jobs),16)
        self.assertTrue(all(j['status']=='running' for j in jobs))
        from collections import Counter
        self.assertEqual(sorted(Counter(g for j in jobs for g in j['allocation']['gpus']).values()),[4]*4)
        self.assertEqual(len({c for j in jobs for c in j['allocation']['cpus']}),32)
        with self.assertRaisesRegex(ValueError,'CPU allocation'):
            fixture.runtime.jobs.submit(session,dict(command=['true'],hours=.1,gpus=0,cpu_threads=33),'oversized-job')

    def test_auto_cpu_allocation_respects_per_job_cap_with_larger_assignment(self):
        policy=dict(self.policy,max_cpu_threads_per_job=32)
        with patch('labcore.resources.snapshot',return_value=dict(cpus=list(range(62)),gpus=[])):
            self.assertEqual(len(resources.allocate(policy,[],dict(gpus=0))['cpus']),32)
            self.assertIsNone(resources.allocate(policy,[],dict(gpus=0,cpu_threads=33)))

    def test_telemetry_exposes_shared_capacity_and_jobs(self):
        gpu=SimpleNamespace(stdout='0, GPU-a, A10, 24000, 20000, 50, 40\n')
        proc=SimpleNamespace(stdout='10, GPU-a\n')
        with patch('labcore.server_status.subprocess.run',side_effect=[gpu,proc]),patch('labcore.resources.process_belongs_to_job',return_value=True):
            result=server_status.view(server_status.collect(self.policy,[self.owner()]))
        card=result['gpus'][0]
        self.assertTrue(card['shared_available'])
        self.assertEqual(card['shared_jobs'],['job'])
        self.assertFalse(card['available_for_lab'])
        self.assertEqual(card['reserved_memory_mib'],4096)
