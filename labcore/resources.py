"""Launch-time CPU and exclusive/shared GPU reservations across all projects."""
import csv
import math
import os
from pathlib import Path
import subprocess


def process_belongs_to_job(pid, job):
    """Only share with live, identity-checked jobs managed by this lab."""
    from .runner import identity
    root = job.get('pid')
    if not root or not job.get('identity') or identity(root) != job['identity']:
        return False
    try:
        current = int(pid)
        for _ in range(128):
            if current == root:
                return True
            if current <= 1:
                return False
            stat = Path(f'/proc/{current}/stat').read_text()
            current = int(stat[stat.rfind(')')+2:].split()[1])
    except (OSError, ValueError, TypeError):
        pass
    return False


def gpu_capacity(policy, jobs, uuid, total, free, utilization, processes):
    if not all(math.isfinite(n) for n in (total, free, utilization)) or total <= 0 or not 0 <= free <= total or not 0 <= utilization <= 100:
        raise ValueError('Invalid GPU capacity telemetry')
    owners = [j for j in jobs if j['status'] in ('running', 'stopping') and uuid in j.get('allocation', {}).get('gpus', [])]
    exclusive = not owners and not processes and free / total >= policy.get('min_gpu_free_fraction', .9) and utilization <= policy.get('max_gpu_utilization', 10)
    sharing = policy.get('gpu_sharing', {})
    reasons = []
    if not sharing.get('enabled', False): reasons.append('GPU sharing disabled')
    if any(j.get('gpu_mode', 'exclusive') != 'shared' for j in owners): reasons.append('Exclusive lab job present')
    if any(not any(process_belongs_to_job(pid, j) for j in owners) for pid in processes): reasons.append('Unmanaged compute process present')
    if not owners and (processes or utilization > policy.get('max_gpu_utilization', 10)): reasons.append('GPU is not idle for its first shared job')
    if len(owners) >= sharing.get('max_jobs_per_gpu', 4): reasons.append('Shared job slots occupied')
    headroom = max(sharing.get('memory_headroom_mib', 1024), total * sharing.get('memory_headroom_fraction', .1))
    reserved = 0
    for job in owners:
        if job.get('gpu_mode', 'exclusive') != 'shared': continue
        memory = job.get('gpu_memory_mib')
        if isinstance(memory, bool) or not isinstance(memory, (int, float)) or not math.isfinite(memory) or memory <= 0:
            reasons.append('Invalid saved shared memory reservation')
        else:
            reserved += memory
    available = max(0, min(total - reserved - headroom, free - headroom))
    if available <= 0: reasons.append('Insufficient shared GPU memory')
    return dict(exclusive_available=exclusive, shared_available=not reasons,
                shared_available_mib=available if not reasons else 0, shared_unavailable_reasons=reasons,
                shared_jobs=[j.get('id') for j in owners if j.get('gpu_mode') == 'shared'], reserved_memory_mib=reserved)


def snapshot(policy, jobs):
    cpus = sorted(os.sched_getaffinity(0))
    active = [j for j in jobs if j['status'] in ('running', 'stopping')]
    reserved = {c for j in active for c in j.get('allocation', {}).get('cpus', [])}
    occupied = max(math.ceil(os.getloadavg()[0]), len(reserved))
    count = max(0, len(cpus) - occupied - policy.get('reserve_cpu_threads', 2))
    result = dict(cpus=[c for c in cpus if c not in reserved][:count], gpus=[], gpu_details={}, gpu_error=None)
    try:
        gpu = subprocess.run(['nvidia-smi', '--query-gpu=index,uuid,memory.total,memory.free,utilization.gpu',
                              '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5, check=True)
        proc = subprocess.run(['nvidia-smi', '--query-compute-apps=pid,gpu_uuid', '--format=csv,noheader'],
                              capture_output=True, text=True, timeout=5, check=True)
        processes = {}
        for row in csv.reader(proc.stdout.splitlines()):
            # Unknown/unavailable PIDs remain busy and cannot authorize sharing.
            if len(row) == 2: pid, uuid = [s.strip() for s in row]
            elif len(row) == 1: pid, uuid = None, row[0].strip()
            else: raise ValueError('Invalid GPU process telemetry')
            processes.setdefault(uuid, []).append(pid)
        allowed = policy.get('allowed_gpus', [])
        for row in csv.reader(gpu.stdout.splitlines()):
            index, uuid, total, free, utilization = [v.strip() for v in row]
            total, free, utilization = float(total), float(free), float(utilization)
            if not all(math.isfinite(n) for n in (total, free, utilization)) or total <= 0:
                raise ValueError('Invalid GPU capacity telemetry')
            if allowed != 'all' and index not in {str(g) for g in allowed}: continue
            details = gpu_capacity(policy, jobs, uuid, total, free, utilization, processes.get(uuid, []))
            result['gpu_details'][uuid] = details
            if details['exclusive_available']: result['gpus'].append(uuid)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        result.update(gpus=[], gpu_details={}, gpu_error=str(e))
    return result


def allocate(policy, jobs, request):
    free = snapshot(policy, jobs)
    cpu = request.get('cpu_threads')
    cpu = min(len(free['cpus']), policy.get('max_cpu_threads_per_job', 32)) if cpu is None else int(cpu)
    count = int(request.get('gpus', 0))
    shared = request.get('gpu_mode', 'exclusive') == 'shared'
    candidates = [g for g, d in free.get('gpu_details', {}).items()
                  if d['shared_available'] and d['shared_available_mib'] >= request['gpu_memory_mib']] if shared else free['gpus']
    existing = set(policy.get('assignment_gpu_ids', []))
    candidates.sort(key=lambda g: (g not in existing, -len(free.get('gpu_details', {}).get(g, {}).get('shared_jobs', []))))
    chosen = []
    for gpu in candidates:
        if len(chosen) >= count: break
        if len(existing | set(chosen) | {gpu}) <= policy.get('assignment_max_gpus', math.inf): chosen.append(gpu)
    if cpu < 1 or cpu > min(len(free['cpus']),policy.get('max_cpu_threads_per_job',32)) or count > len(chosen): return None
    return dict(cpus=free['cpus'][:cpu], gpus=chosen, gpu_mode='shared' if shared else 'exclusive',
                gpu_memory_mib=request.get('gpu_memory_mib') if shared else None)
