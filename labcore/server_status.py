"""Read-only hardware telemetry; no model calls or experiment allocations."""
import csv
import math
import os
import subprocess
import time
from .resources import gpu_capacity
from pathlib import Path


def collect(policy, jobs, previous=None):
    now=time.time()
    previous=previous or {}
    cpus=sorted(os.sched_getaffinity(0))
    active=[j for j in jobs if j['status'] in ('running','stopping')]
    reserved_cpus={c for j in active for c in j.get('allocation',{}).get('cpus',[])}
    reserved_gpus={g for j in active for g in j.get('allocation',{}).get('gpus',[])}
    load=list(os.getloadavg())
    available=max(0,len(cpus)-max(math.ceil(load[0]),len(reserved_cpus))-policy.get('reserve_cpu_threads',2))
    result=dict(id='server_status',observed_at=now,cpu=dict(threads=len(cpus),lab_reserved_threads=len(reserved_cpus & set(cpus)),
        schedulable_threads=min(available,len(set(cpus)-reserved_cpus)),load_average=load,utilization_percent=None),memory={},gpus=[],gpu_error=None)
    try:
        total=idle=0
        for line in Path('/proc/stat').read_text().splitlines():
            parts=line.split()
            if parts and parts[0].startswith('cpu') and parts[0][3:].isdigit() and int(parts[0][3:]) in cpus:
                values=list(map(int,parts[1:9]));total+=sum(values);idle+=values[3]+values[4]
        result['_cpu_ticks']=dict(total=total,idle=idle)
        old=previous.get('_cpu_ticks',{})
        delta=total-old.get('total',total)
        if delta>0:
            result['cpu']['utilization_percent']=round(max(0,min(100,100*(1-(idle-old.get('idle',idle))/delta))),1)
        memory={line.split(':',1)[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.split(':',1)[0] in ('MemTotal','MemAvailable')}
        if memory.get('MemTotal') and 'MemAvailable' in memory:
            result['memory']=dict(total_bytes=memory['MemTotal'],available_bytes=memory['MemAvailable'],used_bytes=memory['MemTotal']-memory['MemAvailable'])
    except (OSError,ValueError,IndexError):
        result['cpu_error']='CPU or memory metrics unavailable'
    try:
        gpu=subprocess.run(['nvidia-smi','--query-gpu=index,uuid,name,memory.total,memory.free,utilization.gpu,temperature.gpu','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=5,check=True)
        processes=subprocess.run(['nvidia-smi','--query-compute-apps=pid,gpu_uuid','--format=csv,noheader'],capture_output=True,text=True,timeout=5,check=True)
        process_map={}
        for row in csv.reader(processes.stdout.splitlines()):
            if len(row)==2:pid,uuid=[v.strip() for v in row]
            elif len(row)==1:pid,uuid=None,row[0].strip()
            else:raise ValueError('Invalid GPU process telemetry')
            process_map.setdefault(uuid,[]).append(pid)
        busy=set(process_map)
        allowed=policy.get('allowed_gpus',[])
        for row in csv.reader(gpu.stdout.splitlines()):
            index,uuid,name,total,free,utilization,temperature=[s.strip() for s in row]
            total=float(total);free=float(free);utilization=float(utilization)
            eligible=allowed=='all' or index in {str(g) for g in allowed}
            capacity=gpu_capacity(policy,jobs,uuid,total,free,utilization,process_map.get(uuid,[]))
            if not eligible:
                capacity.update(shared_available=False,shared_available_mib=0,shared_unavailable_reasons=['Not enabled for lab'])
            reasons=[]
            if not eligible:reasons.append('Not enabled for lab')
            if uuid in reserved_gpus:reasons.append('Reserved by lab job')
            if uuid in busy:reasons.append('Compute process present')
            if total<=0 or free/total<policy.get('min_gpu_free_fraction',.9):reasons.append('Below required free memory')
            if utilization>policy.get('max_gpu_utilization',10):reasons.append('Above idle utilization threshold')
            result['gpus'].append(dict(capacity,uuid=uuid,index=int(index),name=name,memory_total_mib=total,memory_used_mib=total-free,
                memory_free_mib=free,utilization_percent=utilization,temperature_c=float(temperature) if temperature not in ('N/A','[N/A]','[Not Supported]') else None,
                lab_reserved=uuid in reserved_gpus,available_for_lab=not reasons,unavailable_reasons=reasons))
    except (OSError,ValueError,subprocess.SubprocessError):
        result['gpus']=[]
        result['gpu_error']='GPU telemetry unavailable (nvidia-smi failed or timed out)'
    return result


def view(state, now=None, max_age=45):
    now=time.time() if now is None else now
    observed=state.get('observed_at')
    age=max(0,int(now-observed)) if observed else None
    def pick(value,keys):return {key:value[key] for key in keys.split() if key in value}
    return dict(status='unavailable' if age is None else 'stale' if age>max_age else 'ok',observed_at=observed,age_seconds=age,
        cpu=pick(state.get('cpu',{}),'threads lab_reserved_threads schedulable_threads load_average utilization_percent'),
        memory=pick(state.get('memory',{}),'total_bytes available_bytes used_bytes'),
        gpus=[pick(g,'uuid shared_available shared_available_mib shared_jobs reserved_memory_mib shared_unavailable_reasons index name memory_total_mib memory_used_mib memory_free_mib utilization_percent temperature_c lab_reserved available_for_lab unavailable_reasons') for g in state.get('gpus',[])],
        gpu_error=state.get('gpu_error'),cpu_error=state.get('cpu_error'))
