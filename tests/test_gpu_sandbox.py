"""GPU compatibility exceptions retain filesystem and process boundaries."""
import subprocess
import sys
import unittest
from pathlib import Path

import test_platform as fixtures


class GPUSandboxTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    cleanup_processes = fixtures.PlatformTests.cleanup_processes
    session = fixtures.PlatformTests.session

    def execute(self, code, gpu):
        session=self.session();session['gpu_job']=gpu
        workspace=Path(session['workspace'])
        env=self.runtime.runner.environment(workspace)
        command=self.runtime.runner.sandbox_command([sys.executable,'-c',code],session,env)
        return subprocess.run(command,env=env,cwd=workspace,capture_output=True,text=True)

    def test_new_gpu_threads_can_set_comm_but_cpu_sessions_cannot(self):
        code='''import pathlib,threading
errors=[]
def worker():
 try:pathlib.Path('/proc/self/task/'+str(threading.get_native_id())+'/comm').write_text('cuda-worker')
 except OSError:errors.append(1)
t=threading.Thread(target=worker);t.start();t.join()
assert not errors, 'Thread comm write denied'
'''
        result=self.execute(code,True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotEqual(self.execute(code,False).returncode,0)

    def test_gpu_exception_does_not_open_other_proc_or_project_writes(self):
        code='''import pathlib,os
for name in ['/proc/self/oom_score_adj','/proc/'+str(os.getppid())+'/task/'+str(os.getppid())+'/comm']:
 try:
  fd=os.open(name,os.O_WRONLY);os.close(fd)
 except PermissionError:continue
 raise AssertionError('Unexpected write permission: '+name)
'''
        result=self.execute(code,True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_gpu_permission_does_not_extend_to_forked_child(self):
        code='''import os,pathlib
pid=os.fork()
if pid==0:
 try:pathlib.Path('/proc/self/task/'+str(os.getpid())+'/comm').write_text('child');os._exit(2)
 except PermissionError:os._exit(0)
_,status=os.waitpid(pid,0)
assert os.waitstatus_to_exitcode(status)==0
'''
        result=self.execute(code,True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_compilation_caches_stay_in_workspace(self):
        session=self.session()
        env=self.runtime.runner.environment(session['workspace'])
        for key in ['TRITON_CACHE_DIR','TORCHINDUCTOR_CACHE_DIR','CUDA_CACHE_PATH','MPLCONFIGDIR']:
            self.assertTrue(Path(env[key]).is_relative_to(Path(session['workspace'])))
            self.assertTrue(Path(env[key]).is_dir())

    def test_triton_cache_publication_works_in_abi1_sandbox(self):
        import importlib.util
        if importlib.util.find_spec('triton') is None:
            self.skipTest('Triton is not installed')
        result=self.execute('''from triton.runtime.cache import get_cache_manager
from pathlib import Path
cache=get_cache_manager('ab'*32)
path=cache.put(b'compiled-binary','module.so')
assert Path(path).read_bytes()==b'compiled-binary'
cache.put_group('module',{'module.so':path})
assert cache.get_group('module')['module.so']==path
print('cache publication and group index succeeded')
''',False)
        self.assertEqual(result.returncode,0,result.stderr)
