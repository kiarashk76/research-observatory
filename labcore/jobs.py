"""Detached, resource-limited experiments; requests originate in session control files."""
import json
import math
import os
import shlex
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

from . import resources
from .files import atomic_json, digest, scoped
from .runner import identity
from .store import uid, utc


class Jobs:
    def __init__(self, engine, runner):
        self.engine, self.runner = engine, runner
        self.processes = {}

    def submit(self, session, request, key):
        cfg = self.engine.cfg
        with self.engine.store.tx() as tx:
            duplicate = tx.get("job_requests", key)
            if duplicate:
                return tx.get("jobs", duplicate["job"])
            current = tx.get("sessions", session["id"])
            task = tx.get("tasks", session.get("task", ""))
            if current["status"] not in ("running", "stopping") or not task or session["role"] == "supervisor":
                raise ValueError("Only an active specialist/worker session may request an experiment")
            from .documentation_policy import is_documentation
            if cfg.get('documentation', {}).get('enabled', False) and is_documentation(task):
                if int(request.get('gpus', 0)) or float(request.get('hours', 0)) > cfg['documentation'].get('max_build_minutes', 10)/60:
                    raise ValueError('Documentation jobs are CPU-only bounded builds/checks; use a separate research assignment for experiments')
            root = tx.get("tasks", task["root_task"])
            bound = root["resources"]
            hours = float(request["hours"])
            raw_gpus = request.get('gpus', 0)
            if isinstance(raw_gpus, bool) or not isinstance(raw_gpus, (int, float)) or not math.isfinite(raw_gpus) or int(raw_gpus) != raw_gpus:
                raise ValueError('gpus must be a finite integer')
            gpus = int(raw_gpus)
            mode = request.get('gpu_mode', 'exclusive')
            memory = request.get('gpu_memory_mib')
            if mode not in ('exclusive', 'shared'):
                raise ValueError('gpu_mode must be exclusive or shared')
            if mode == 'shared':
                if not cfg['resources'].get('gpu_sharing', {}).get('enabled', False):
                    raise ValueError('GPU sharing is disabled')
                if gpus < 1 or isinstance(memory, bool) or not isinstance(memory, (int, float)) or not math.isfinite(memory) or memory <= 0:
                    raise ValueError('Shared GPU jobs require GPUs and positive finite gpu_memory_mib per GPU')
            elif memory is not None:
                raise ValueError('gpu_memory_mib requires gpu_mode=shared')
            cpus = request.get("cpu_threads")
            if not 0 < hours <= bound["max_job_hours"] or gpus < 0 or gpus > bound["max_gpus"]:
                raise ValueError("Experiment exceeds the supervisor's time/GPU allocation")
            if cpus is not None and (isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not math.isfinite(cpus) or int(cpus) != cpus or not 1 <= cpus <= min(bound['max_cpu_threads'],cfg['resources']['max_cpu_threads_per_job'])):
                raise ValueError("Experiment exceeds the supervisor's CPU allocation")
            siblings = [j for j in tx.all("jobs") if j["root_task"] == root["id"] and j["status"] != "cancelled"]
            if sum(j["hours"] for j in siblings) + hours > bound["total_job_hours"]:
                raise ValueError("Assignment compute-hour allowance exhausted; request more from the supervisor")
            if sum(j["status"] in ("queued", "running", "approval") for j in siblings) >= bound["max_jobs"]:
                raise ValueError("Assignment experiment concurrency/queue allowance exhausted")
            cwd = scoped(session["workspace"], request.get("cwd", "."))
            if not cwd.is_dir():
                raise ValueError("Experiment cwd must be a directory in this assignment")
            command = request["command"]
            if not isinstance(command, (list, str)) or not command:
                raise ValueError("Experiment needs a command")
            configs = []
            for path in request.get("configs", []):
                source = scoped(session["workspace"], path)
                configs.append({"path": str(source), "sha256": digest(source)})
            metrics = str(scoped(session["workspace"], request["metrics"], exists=False)) if request.get("metrics") else None
            if metrics and any(j.get("metrics") == metrics and j["status"] in ("queued", "running", "approval") for j in tx.all("jobs")):
                raise ValueError("Concurrent experiments must use separate metrics files")
            job_id = uid()
            project = tx.get("projects", session["project"])
            run_dir = self.engine.root / "projects" / project["folder"] / "runs" / job_id
            job = {"id": job_id, "study_id": task.get("study_id"), "task": task["id"], "root_task": root["id"], "project": session["project"],
                   "channel": session["channel"], "thread": session.get("thread"), "session": session["id"],
                   "name": request.get("name", "Experiment"), "command": command, "cwd": str(cwd),
                   "workspace": session["workspace"], "hours": hours, "gpus": gpus,
                   "gpu_mode": mode, "gpu_memory_mib": memory,
                   "cpu_threads": int(cpus) if cpus else None, "seeds": request.get("seeds", []),
                   "configs": configs, "metrics": metrics, "run_dir": str(run_dir), "created": time.time(),
                   "status": "approval" if hours > cfg["budgets"]["expensive_job_hours"] else "queued"}
            tx.put("jobs", job)
            tx.put("job_requests", {"id": key, "job": job_id})
            tx.event("experiment_requested", job=job_id, task=task["id"], hours=hours)
            if job["status"] == "approval":
                blocks = [{"type": "section", "text": {"type": "mrkdwn", "text":
                           f"This experiment needs your approval: *{job['name']}* (estimated {hours:g} hours)."}},
                          {"type": "actions", "elements": [
                              {"type": "button", "text": {"type": "plain_text", "text": "Approve experiment"},
                               "action_id": "approve_job", "value": job_id},
                              {"type": "button", "text": {"type": "plain_text", "text": "Decline"},
                               "action_id": "deny_job", "value": job_id}]}]
                tx.put("outbox", {"id": "approval:" + job_id, "channel": job["channel"], "thread": job["thread"],
                                  "text": f"May I run {job['name']}? Its estimate is {hours:g} hours.", "blocks": blocks,
                                  "urgent": False, "status": "queued", "created": time.time()})
        return job

    def control_requests(self):
        for session in self.engine.store.all("sessions"):
            if session["status"] not in ("running", "stopping"):
                continue
            control = Path(session["workspace"]) / "control"
            for path in control.glob("job-*.json"):
                if path.name.endswith(".receipt.json"):
                    continue
                receipt = path.with_name(path.stem + ".receipt.json")
                if receipt.exists():
                    continue
                try:
                    if path.is_symlink() or path.stat().st_size > 65536:
                        raise ValueError("Invalid control request file")
                    job = self.submit(session, json.loads(path.read_text()), str(session.get("task") or session["id"]) + ":" + path.name)
                    atomic_json(receipt, {"ok": True, "job": job["id"], "status": job["status"], "run_dir": job["run_dir"]})
                except (OSError, ValueError, KeyError) as e:
                    atomic_json(receipt, {"ok": False, "error": str(e)})

    def approve(self, job_id, approve=True):
        with self.engine.store.tx() as tx:
            job = tx.get("jobs", job_id)
            if not job or job["status"] != "approval":
                raise ValueError("This experiment is no longer waiting for approval")
            job["status"] = "queued" if approve else "cancelled"
            job["human_approved"] = approve
            tx.put("jobs", job)
            task = tx.get("tasks", job["task"])
            if not approve:
                self.engine.notify_supervisor(tx, task, "The human declined the proposed experiment: " + job["name"])
            tx.event("experiment_approval", job=job_id, approved=approve)

    def start_ready(self):
        if self.engine.store.get("settings", "pause", {}).get("paused"):
            return
        with self.engine.store.tx() as tx:
            jobs = tx.all("jobs")
            running = [j for j in jobs if j["status"] in ("running", "stopping")]
            for job in jobs:
                if job["status"] != "queued":
                    continue
                if len(running) >= self.engine.cfg["budgets"]["max_jobs"]:
                    job["launch_hold"] = {"reason": "All experiment job slots are occupied"}
                    tx.put("jobs", job)
                    continue
                task = tx.get("tasks", job["task"])
                root = tx.get("tasks", job["root_task"])
                project = tx.get("projects", job["project"])
                if task["status"] in ("cancelled", "rejected"):
                    job["status"] = "cancelled"
                    tx.put("jobs", job)
                    continue
                if project["status"] != "active":
                    job["launch_hold"] = {"reason": "Project is " + project["status"]}
                    tx.put("jobs", job)
                    continue
                own_running = [j for j in running if j["root_task"] == root["id"]]
                owned_gpus = {g for j in own_running for g in j["allocation"]["gpus"]}
                cpu_left = root["resources"]["max_cpu_threads"] - sum(len(j["allocation"]["cpus"]) for j in own_running)
                if job["gpus"] > root["resources"]["max_gpus"] or cpu_left < 1 or job["cpu_threads"] and job["cpu_threads"] > cpu_left:
                    job["launch_hold"] = {"reason": "Waiting for this assignment's GPU/CPU allocation to become available"}
                    tx.put("jobs", job)
                    continue
                policy = dict(self.engine.cfg["resources"], max_cpu_threads_per_job=min(cpu_left,self.engine.cfg["resources"]["max_cpu_threads_per_job"]),
                              assignment_gpu_ids=list(owned_gpus), assignment_max_gpus=root['resources']['max_gpus'])
                allocation = resources.allocate(policy, jobs, job)
                if allocation is None:
                    job["launch_hold"] = {"reason": "Waiting for available server GPU/CPU capacity"}
                    tx.put("jobs", job)
                    continue
                job.pop("launch_hold", None)
                try:
                    self._launch(job, allocation)
                except (OSError, ValueError, subprocess.SubprocessError) as e:
                    job.update(status="failed", error=str(e), ended=time.time())
                    tx.put("jobs", job)
                    self.engine.notify_supervisor(tx, task, "An experiment could not start: " + str(e), key="launch-failed:" + job["id"])
                    continue
                tx.put("jobs", job)
                running.append(job)

    def _launch(self, job, allocation):
        run_dir = Path(job["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        # Preserve actual config inputs, not only hashes.
        for index, config in enumerate(job["configs"]):
            source = Path(config["path"])
            saved = run_dir / "configs" / f"{index}-{source.name}"
            saved.parent.mkdir(exist_ok=True)
            saved.write_bytes(source.read_bytes())
            config.update(sha256=digest(saved), snapshot=str(saved))
        if job["metrics"] and Path(job["metrics"]).exists():
            stat = Path(job["metrics"]).stat()
            job["metrics_before"] = [stat.st_mtime_ns, stat.st_size]
        origin = self.engine.store.get("sessions", job.get("session", ""), {})
        env = self.runner.environment(job["workspace"], origin.get("account"))
        count = str(len(allocation["cpus"]))
        env.update(CUDA_VISIBLE_DEVICES=",".join(allocation["gpus"]), OMP_NUM_THREADS=count,
                   MKL_NUM_THREADS=count, OPENBLAS_NUM_THREADS=count, LAB_CPU_THREADS=count, LAB_GPU_MODE=job.get('gpu_mode', 'exclusive'),
                   LAB_GPU_MEMORY_MIB=str(job.get('gpu_memory_mib') or ''))
        command = job["command"]
        command = ["bash", "-c", command] if isinstance(command, str) else command
        if not shutil_available("taskset"):
            raise ValueError("taskset is required for experiment CPU affinity")
        command = ["taskset", "--cpu-list", ",".join(map(str, allocation["cpus"]))] + command
        fake_session = {"project": job["project"], "workspace": job["workspace"], "account": origin.get("account"), "gpu_job": bool(allocation["gpus"])}
        command = self.runner.sandbox_command(command, fake_session, env)
        wrapper = [sys.executable, str(self.engine.root / "labcore" / "jobexec.py"),
                   "--exit-file", str(run_dir / "exit.json"), "--db", str(self.engine.store.path), "--job", job["id"], "--"] + command
        with (run_dir / "output.log").open("w") as log:
            proc = subprocess.Popen(wrapper, cwd=job["cwd"], env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes[job["id"]] = proc
        job.update(status="running", pid=proc.pid, identity=identity(proc.pid), allocation=allocation,
                   started=time.time(), deadline=time.time() + job["hours"] * 3600)
        try:
            git = subprocess.run(["git", "-C", job["cwd"], "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
            job["commit"] = git.stdout.strip() if git.returncode == 0 else None
            dirty = subprocess.run(["git", "-C", job["cwd"], "status", "--porcelain"], capture_output=True, text=True, timeout=5)
            job["dirty"] = bool(dirty.stdout.strip()) if git.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            job["commit"] = None
        atomic_json(run_dir / "manifest.json", job)

    def poll(self):
        finished_tasks = set()
        for job in self.engine.store.all("jobs"):
            if job["status"] not in ("running", "stopping"):
                continue
            exit_file = Path(job["run_dir"]) / "exit.json"
            proc = self.processes.get(job["id"])
            live = proc.poll() is None if proc else identity(job["pid"]) == job.get("identity")
            if job["status"] == "stopping":
                if live:
                    if time.time() >= job["stop_at"] and job.get("identity") and identity(job["pid"]) == job["identity"]:
                        try:
                            os.killpg(job["pid"], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    continue
                if job.get("stop_kind") != "time_limit":
                    job.update(status="cancelled", ended=time.time())
                    with self.engine.store.tx() as tx:
                        tx.put("jobs", job)
                    continue
                # Finalize timeouts through the normal report/notification path.
                atomic_json(exit_file, {"exit_code": 124, "reason": "time limit",
                                        "stop_reason": job["stop_reason"]})
            if live and time.time() > job["deadline"]:
                self.stop(job["id"], "Experiment reached its requested wall-time limit", time_limit=True)
                continue
            if live and not exit_file.exists():
                continue
            self.processes.pop(job["id"], None)
            try:
                code = json.loads(exit_file.read_text())["exit_code"]
            except (OSError, ValueError, KeyError):
                code = -1
            job.update(status="done" if code == 0 else "failed", exit_code=code, ended=time.time())
            report = dict(job)
            if job["metrics"]:
                try:
                    path = Path(job["metrics"])
                    stat = path.stat()
                    if [stat.st_mtime_ns, stat.st_size] == job.get("metrics_before"):
                        raise ValueError("Metrics file is unchanged from before this run")
                    if stat.st_size > 100000:
                        raise ValueError("Metrics file exceeds 100 KiB; save a compact summary")
                    metrics = json.loads(path.read_text())
                    if not isinstance(metrics, dict):
                        raise ValueError("Metrics must be a JSON object")
                    report["metrics"] = metrics
                    report["summaries"] = {}
                    for name, values in metrics.items():
                        if isinstance(values, list) and values and all(type(v) in (int, float) for v in values):
                            report["summaries"][name] = {"n": len(values), "mean": statistics.mean(values),
                                "sample_sd": statistics.stdev(values) if len(values) > 1 else None}
                except (OSError, ValueError) as e:
                    report["metrics_error"] = str(e)
            atomic_json(Path(job["run_dir"]) / "report.json", report)
            with self.engine.store.tx() as tx:
                tx.put("jobs", job)
                finished_tasks.add(job["task"])
                if code != 0:
                    task = tx.get("tasks", job["task"])
                    self.engine.notify_supervisor(tx, task, "An experiment failed: " + job["name"], key="job-failed:" + job["id"])
        with self.engine.store.tx() as tx:
            for task_id in finished_tasks:
                task = tx.get("tasks", task_id)
                remaining = [j for j in tx.all("jobs") if j["task"] == task_id and j["status"] in ACTIVE]
                if not remaining and task["status"] == "waiting_jobs":
                    if task["allow_job_followup"]:
                        task["status"] = "queued"
                        tx.put("tasks", task)
                    else:
                        self.engine.notify_supervisor(tx, task, "The experiment group finished and needs an analysis assignment.")

    def stop(self, job_id, reason="Stopped by the human", *, time_limit=False):
        with self.engine.store.tx() as tx:
            job = tx.get("jobs", job_id)
            if not job:
                raise ValueError("Unknown experiment")
            if job["status"] in ("done", "failed", "cancelled"):
                return
            running = job["status"] in ("running", "stopping")
            # Persist intent before signaling, so daemon restart preserves the cause.
            if job["status"] == "stopping":
                return
            grace = 60 if time_limit else 10
            job.update(status="stopping" if running else "cancelled", stop_reason=reason,
                       stop_kind="time_limit" if time_limit else "cancelled",
                       stop_at=time.time() + grace)
            if not running:
                job["ended"] = time.time()
            tx.put("jobs", job)
        if running and job.get("identity") and identity(job["pid"]) == job["identity"]:
            try:
                os.killpg(job["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass



ACTIVE = {"running", "queued", "approval", "stopping"}


def shutil_available(name):
    import shutil
    return shutil.which(name)
