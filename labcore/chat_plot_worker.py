"""One isolated CPU-only plotting process; no network, child processes or source writes."""
import ctypes
import errno
import json
import os
from pathlib import Path
import resource
import runpy
import signal
import sys

if __package__ in (None,''):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from labcore.sandbox import restrict


def no_network_or_processes():
    machine=os.uname().machine
    if machine=='x86_64':
        arch=0xc000003e;blocked=list(range(41,56))+[56,57,58,59,62,101,165,166,234,272,288,303,304,308,310,311,157,317,321,322,425,426,427,435]
    elif machine=='aarch64':
        arch=0xc00000b7;blocked=list(range(198,213))+[40,41,97,117,129,131,220,221,242,264,265,268,270,271,167,277,280,281,282,425,426,427,435]
    else:raise RuntimeError('Plot sandbox does not support this CPU architecture')
    class Filter(ctypes.Structure):_fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint32)]
    class Program(ctypes.Structure):_fields_=[('length',ctypes.c_ushort),('filter',ctypes.POINTER(Filter))]
    deny=0x00050000|errno.EPERM
    rules=[(0x20,0,0,4),(0x15,1,0,arch),(0x06,0,0,deny),(0x20,0,0,0),(0x35,0,1,0x40000000),(0x06,0,0,deny)]
    for number in blocked:rules.extend([(0x15,0,1,number),(0x06,0,0,deny)])
    rules.append((0x06,0,0,0x7fff0000));array=(Filter*len(rules))(*(Filter(*r) for r in rules));program=Program(len(rules),array)
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(38,1,0,0,0) or libc.prctl(22,2,ctypes.byref(program),0,0):raise OSError(ctypes.get_errno(),'Cannot enforce plotting seccomp filter')


def main():
    parent=os.getppid();libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(1,signal.SIGKILL,0,0,0) or os.getppid()!=parent:raise RuntimeError('Cannot bind plotting process to its parent')
    manifest=json.loads(Path(sys.argv[1]).read_text());work=Path(manifest['work']);output=work/'output'
    resource.setrlimit(resource.RLIMIT_CPU,(30,30));resource.setrlimit(resource.RLIMIT_AS,(2*1024**3,2*1024**3));resource.setrlimit(resource.RLIMIT_FSIZE,(24*1024**2,24*1024**2));resource.setrlimit(resource.RLIMIT_NOFILE,(64,64));resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    prefix=Path(sys.prefix).resolve()
    reads=['/usr','/lib','/lib64','/etc/fonts','/etc/ld.so.cache','/etc/localtime','/dev/null','/dev/urandom','/proc/cpuinfo','/proc/meminfo','/sys/devices/system/cpu',prefix,work/'input',work/'plot.py']
    restrict(reads,[output,work/'tmp','/dev/null'])
    no_network_or_processes();os.chdir(output)
    runpy.run_path(str(work/'plot.py'),init_globals={'INPUTS':manifest['inputs'],'OUTPUT_DIR':str(output)},run_name='__main__')
if __name__=='__main__':main()
