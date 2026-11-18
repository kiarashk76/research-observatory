"""Deterministic scheduler. Idle polling never calls a model."""
import json
import time
import uuid
import datetime as dt
from zoneinfo import ZoneInfo
from pathlib import Path

from .files import atomic_json
from .jobs import Jobs
from .runner import Runner
from .store import uid
from . import subscription, wakeups, auth, accounts, history, server_status


class Runtime:
    def __init__(self, engine):
        self.engine = engine
        self.runner = Runner(engine)
        self.jobs = Jobs(engine, self.runner)
        self.subscription = subscription.Monitor(engine)
        self.last_error = None

    def tick(self):
        # Ingest controls before final results so an exiting worker's last job is not lost.
        for callback in (self.recover_auth, self.fire_wakes, self.subscription.refresh, self.jobs.control_requests, self.runner.poll, self.jobs.poll, self.collect_history, self.reconcile,
                         self.preempt, self.dispatch, self.jobs.start_ready, self.status_views):
            try:
                callback()
            except Exception as e:
                self.last_error = f"{callback.__name__}: {type(e).__name__}: {e}"
                with self.engine.store.tx() as tx:
                    tx.put("settings", {"id": "runtime_error", "error": self.last_error, "time": time.time()})

    def collect_history(self):
        history.collect(self.engine)

    def fire_wakes(self):
        wakeups.fire(self.engine)

    def recover_auth(self):
        auth.recover(self.engine)

    def reconcile(self):
        with self.engine.store.tx() as tx:
            tasks = tx.all("tasks")
            active_tasks = {s.get("task") for s in tx.all("sessions") if s["status"] in ("running", "stopping")}
            for task in tasks:
                if task["status"] == "queued" and task["id"] in active_tasks:
                    task["status"] = "running"
                    task.pop("launch_hold", None)
                    tx.put("tasks", task)
                if task['status'] == 'deferred' and task.get('not_before', float('inf')) <= time.time():
                    task['status'] = 'queued'
                    tx.put('tasks', task)
                if task["status"] == "waiting_children":
                    children = [t for t in tasks if t.get("parent") == task["id"]]
                    if children and all(t["status"] == "accepted" for t in children) and task["allow_child_followup"]:
                        task["status"] = "queued"
                        tx.put("tasks", task)
                elif task["status"] == "waiting_jobs":
                    jobs = [j for j in tx.all("jobs") if j.get("task") == task["id"]]
                    if jobs and all(j["status"] not in ("running", "queued", "approval", "stopping") for j in jobs):
                        if task["allow_job_followup"]:
                            task["status"] = "queued"
                            tx.put("tasks", task)
                        else:
                            self.engine.notify_supervisor(tx, task, "Experiment group finished; analysis needs authorization.", key="group:" + task["id"] + ":" + str(task["attempts"]))

    def preempt(self):
        from .documentation_policy import launch_issue
        with self.engine.store.tx() as tx:
            tasks = tx.all("tasks")
            queued = [t for t in tasks if t["status"] == "queued"
                      and not subscription.gate(self.engine, t['model'], t.get('usage_class', 'large'))]
            for session in tx.all("sessions"):
                if session["status"] != "running":
                    continue
                task = tx.get("tasks", session.get("task", ""))
                project = tx.get("projects", session["project"])
                cancel = task and task["status"] == "cancelled"
                pause = project["status"] != "active" and session["role"] != "supervisor"
                pause = pause or bool(tx.get("closed_threads", session["channel"] + ":" + str(session.get("thread"))))
                pause = pause or not tx.get("channels", session["channel"], {}).get("active", False)
                urgent = any(t["role"] == session["role"] and t["priority"] >= self.engine.cfg["scheduling"].get("preemption_priority", 90)
                             and t["priority"] > session["priority"]
                             and not launch_issue(self.engine,tx,t,ignore_session=session["id"]) for t in queued)
                if session["role"] == "supervisor":
                    urgent = any(r["status"] == "queued" and r["priority"] > session["priority"] for r in tx.all("requests"))
                if cancel or pause or urgent:
                    session.update(preempted=True, stop_reason="Cancelled/paused" if cancel or pause else "Higher-priority work is waiting")
                    tx.put("sessions", session)

    def dispatch(self):
        if not accounts.profiles(self.engine.cfg) and self.engine.store.get('settings', 'authentication', {}).get('required'):
            return
        cooldown = self.engine.store.get("settings", "cooldown", {})
        if (not accounts.profiles(self.engine.cfg) and cooldown.get("until", 0) > time.time()) or self.engine.store.get("settings", "pause", {}).get("paused"):
            return
        launched = []
        with self.engine.store.tx() as tx:
            active = [s for s in tx.all("sessions") if s["status"] in ("running", "stopping")]
            slots = self.engine.cfg["scheduling"]["max_sessions"] - len(active)
            if slots <= 0:
                return
            if not any(s["role"] == "supervisor" for s in active):
                pending = [r for r in tx.all("requests") if r["status"] == "queued"
                           and tx.get("channels", r["payload"].get("channel"), {}).get("active", False)
                           and not tx.get("closed_threads", r["payload"].get("channel", "") + ":" + str(r["thread"]))
                           and (tx.get("projects", r["project"])["status"] == "active" or r["priority"] >= 100)
                           and time.time() - r["created"] >= self.engine.cfg["scheduling"].get("debounce_seconds", 2)]
                pending.sort(key=lambda r: (-r["priority"], r["created"]))
                if pending:
                    first = pending[0]
                    project = tx.get("projects", first["project"])
                    group = [r for r in pending if r["project"] == first["project"] and r["thread"] == first["thread"]]
                    onboarding = all(r["payload"].get("onboarding") for r in group)
                    minutes = min(2, self.engine.cfg["claude"]["supervisor_minutes"]) if onboarding else self.engine.cfg["claude"]["supervisor_minutes"]
                    reason = self.engine.budget_available(tx, project, minutes)
                    model = self.engine.cfg['claude'].get('onboarding_model', 'haiku') if onboarding else self.engine.cfg['claude']['supervisor_model']
                    effort = self.engine.cfg['claude'].get('onboarding_effort', 'low') if onboarding else self.engine.cfg['claude']['supervisor_effort']
                    quota_hold = subscription.gate(self.engine, model, 'supervisor')
                    fallback = self.engine.cfg.get('subscription', {}).get('supervisor_fallback_model')
                    fallback_used = False
                    if quota_hold and fallback and not subscription.gate(self.engine, fallback, 'supervisor'):
                        model = fallback
                        effort = self.engine.cfg['subscription'].get('supervisor_fallback_effort', 'medium')
                        quota_hold = None
                        fallback_used = True
                    if quota_hold:
                        reason = reason or quota_hold['reason']
                    if not reason:
                        sid = str(uuid.uuid4())
                        channel = first["payload"].get("channel") or next(c["id"] for c in tx.all("channels") if c["project"] == project["id"])
                        session = self._session(sid, project["id"], channel, first["thread"], "supervisor",
                            model, effort,
                            minutes, first["priority"], self.engine.root / "workspaces" / project["id"] / ("supervisor-" + sid))
                        if accounts.profiles(self.engine.cfg):
                            session["account"] = accounts.choose(self.engine, model, "supervisor")[0]
                        session.update(request_ids=[r["id"] for r in group], reasons=[r["reason"] for r in group])
                        for request in group:
                            if request.get("last_error"):
                                session["reasons"].append("Your previous response was rejected without applying its actions: " + request["last_error"] + " Correct the invalid action using the current assignment_limits; do not repeat the same invalid response.")
                        session["resume_from"] = list({r["session"] for r in group if r.get("session")})
                        if fallback_used:
                            session['reasons'].append('The normal supervisor model has an exhausted model-specific quota. Using the configured '+model+'/'+effort+' fallback; do not silently downgrade specialist assignments.')
                        for req in group:
                            req.update(status="running", session=sid)
                            tx.put("requests", req)
                        tx.put("sessions", session)
                        launched.append(session)
                        active.append(session)
                        slots -= 1
                    else:
                        if quota_hold:
                            self._subscription_notice(tx, first, quota_hold)
                        else:
                            self._budget_notice(tx, first, reason)
            tasks = [t for t in tx.all("tasks") if t["status"] == "queued"]
            # Aging improves fairness without overriding explicit urgent work.
            tasks.sort(key=lambda t: (-min(89, t["priority"] + int((time.time() - t["created"]) / 600)) if t["priority"] < 90 else -t["priority"], t["created"]))
            for task in tasks:
                if slots <= 0:
                    break
                if (self.engine.cfg["scheduling"].get("reserve_supervisor_slot", True)
                        and self.engine.cfg["scheduling"]["max_sessions"] > 1
                        and not any(s["role"] == "supervisor" for s in active) and slots <= 1):
                    break
                if task["role"] == "worker":
                    if sum(s["role"] == "worker" for s in active) >= self.engine.cfg["scheduling"]["max_workers"]:
                        continue
                elif any(s["role"] == task["role"] for s in active):
                    continue
                project = tx.get("projects", task["project"])
                if project["status"] != "active" or not tx.get("channels", task["channel"], {}).get("active"):
                    continue
                if any(tx.get("tasks", d)["status"] != "accepted" for d in task.get("depends", [])):
                    continue
                root = tx.get("tasks", task["root_task"])
                if root["status"] == "cancelled":
                    task["status"] = "cancelled"
                    tx.put("tasks", task)
                    continue
                launch_hold = self.engine.task_launch_hold(tx, task)
                if launch_hold and not subscription.gate(self.engine, task["model"], task.get("usage_class", "large")) and launch_hold["reason"] not in (
                        "assignment session limit", "assignment aggregate session-time budget",
                        "assignment reported-output-token budget"):
                    task["launch_hold"] = launch_hold
                    tx.put("tasks", task)
                    continue
                minutes = task["session_minutes"]
                hold = subscription.gate(self.engine, task['model'], task.get('usage_class', 'large'))
                if hold:
                    task['subscription_hold'] = hold
                    tx.put('tasks', task)
                    continue
                if task.pop('subscription_hold', None):
                    tx.put('tasks', task)
                reason = self.engine.budget_available(tx, project, minutes, task["root_task"])
                if reason:
                    if reason.startswith("assignment"):
                        task.update(status="needs_resources", last_error=reason)
                        tx.put("tasks", task)
                        self.engine.notify_supervisor(tx, task, "The work order needs more authorization: " + reason,
                                                      key="budget:" + task["id"] + ":" + str(task["attempts"]))
                    continue
                sid = str(uuid.uuid4())
                session = self._session(sid, project["id"], task["channel"], task["thread"], task["role"],
                                        task["model"], task["effort"], minutes, task["priority"], self.engine.workspace(task))
                if accounts.profiles(self.engine.cfg):
                    session["account"] = accounts.choose(self.engine, task["model"], task.get("usage_class", "large"))[0]
                session.update(task=task["id"], root_task=task["root_task"], work_kind=task.get("work_kind"))
                task.update(status="running", attempts=task["attempts"] + 1, launch_hold=None)
                tx.put("tasks", task)
                tx.put("sessions", session)
                launched.append(session)
                active.append(session)
                slots -= 1
        for session in launched:
            try:
                self.runner.start(session)
            except Exception as e:
                self.runner.fail(session, f"Cannot start Claude session: {e}", retry=False)

    def _session(self, sid, project, channel, thread, role, model, effort, minutes, priority, workspace):
        session = {"id": sid, "project": project, "channel": channel, "thread": thread, "role": role,
                "model": model, "effort": effort, "minutes": minutes, "priority": priority,
                "workspace": str(workspace), "status": "running", "created": time.time(),
                "request_ids": [], "reasons": [], "applied": False}
        session['provider'] = 'claude'
        return session

    def _budget_notice(self, tx, request, reason):
        # One notice per thread/day/reason rather than a notification on every tick.
        key = "budget-notice:" + request["project"] + ":" + str(request["thread"]) + ":" + str(int(time.time() // 86400)) + ":" + reason
        if not tx.get("outbox", key):
            tx.put("outbox", {"id": key, "channel": request["payload"]["channel"], "thread": request["thread"],
                              "text": f"This work is saved and queued, but the {reason} is exhausted. "
                                      "I’ll resume when the allowance resets or you increase it in the lab configuration.",
                              "status": "queued", "urgent": False, "created": time.time()})

    def _subscription_notice(self, tx, request, hold):
        period = str(int(hold['retry_at'] // 300)) if hold['reset_known'] else 'unknown-' + str(int(time.time() // 86400))
        key = 'subscription-notice:' + request['project'] + ':' + str(request['thread']) + ':' + ','.join(hold['windows']) + ':' + period
        if tx.get('outbox', key):
            return
        when = dt.datetime.fromtimestamp(hold['retry_at'], ZoneInfo(self.engine.cfg.get('timezone', 'UTC'))).strftime('%b %d, %I:%M %p %Z')
        text = ('The relevant Claude subscription window is exhausted. Your message and unfinished work are saved. '
                + (f'I’ll resume after the reported reset around {when}.' if hold['reset_known'] else
                   'No reset time is available; the runtime will check account metadata again without waking a model.'))
        tx.put('outbox', {'id': key, 'channel': request['payload']['channel'], 'thread': request['thread'], 'text': text,
                          'status': 'queued', 'urgent': False, 'created': time.time()})

    def status_views(self):
        telemetry = None
        policy = self.engine.cfg.get('server_status', {})
        prior = self.engine.store.get('settings', 'server_status', {})
        if policy.get('enabled', True) and time.time() - prior.get('observed_at', 0) >= policy.get('poll_seconds', 15):
            telemetry = server_status.collect(self.engine.cfg['resources'], self.engine.store.all('jobs'), prior)
        logins = {a["id"]: auth.source_state(accounts.directory(self.engine.cfg, a["id"]) / ".credentials.json")["usable"]
                  for a in accounts.profiles(self.engine.cfg)}
        with self.engine.store.tx() as tx:
            if telemetry is not None:
                tx.put("settings", telemetry)
            tx.put("settings", dict(id="account_logins", logins=logins, updated=time.time()))
            for task in tx.all("tasks"):
                hold = self.engine.task_launch_hold(tx, task)
                if task.get("launch_hold") != hold:
                    task["launch_hold"] = hold
                    tx.put("tasks", task)
            for project in tx.all("projects"):
                tx.put("scheduler", {"id": project["id"], "project": project["id"],
                                     "budget_usage": self.engine.budget_usage(tx, project), "updated": time.time()})
        sessions = self.engine.store.all("sessions")
        for session in sessions:
            if session["status"] in ("running", "stopping") and Path(session["workspace"]).exists():
                atomic_json(Path(session["workspace"]) / "control" / "status.json", self.engine.context(session))
        for project in self.engine.store.all("projects"):
            base = self.engine.root / "projects" / project["folder"] / "notes" / "conversations"
            grouped = {}
            for message in self.engine.store.all("messages"):
                if message["project"] == project["id"]:
                    grouped.setdefault(message["channel"] + "-" + message["thread"], []).append(message)
            base.mkdir(parents=True, exist_ok=True)
            for key, messages in grouped.items():
                text = "\n\n".join(m["user"] + ":\n" + m["text"] for m in messages)
                path = base / (key.replace("/", "-") + ".md")
                if not path.exists() or path.read_text() != text:
                    path.write_text(text)
