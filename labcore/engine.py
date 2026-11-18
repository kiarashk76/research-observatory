"""Supervisor authority, work orders, evidence, and durable routing."""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import shutil
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import profile
from .files import atomic_json, digest, project_layout, scoped, slug, paper_folder
from .store import Store, uid, utc
from . import subscription, wakeups, delegation, accounts, history, customization

ACTIVE_JOBS = {"queued", "running", "approval", "stopping"}
TERMINAL_TASKS = {"accepted", "cancelled", "rejected"}


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = Path(cfg["root"])
        for folder in ("runtime", "projects", "workspaces", "library/papers", "library/tools"):
            (self.root / folder).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = Store(self.root / "runtime" / "lab.sqlite3")

    def enroll(self, channel_id, name, topic="", purpose=""):
        with self.store.tx() as tx:
            existing = tx.get("channels", channel_id)
            if existing:
                existing.update(name=name, topic=topic, purpose=purpose, active=True)
                tx.put("channels", existing)
                return existing
            project_id = uuid.uuid5(uuid.NAMESPACE_URL, "research-lab:" + channel_id).hex
            project = {"id": project_id, "name": name, "folder": slug(name) + "-" + project_id[:8],
                       "mode": "research", "purpose": purpose or topic, "status": "active", "human_started": False,
                       "daily_minutes": self.cfg["budgets"]["project_daily_minutes"], "brief": "", "created": utc()}
            channel = {"id": channel_id, "project": project_id, "name": name, "topic": topic,
                       "purpose": purpose, "active": True}
            tx.put("projects", project)
            tx.put("channels", channel)
            tx.event("channel_enrolled", channel=channel_id, project=project_id)
        project_layout(self.root, project)
        self.store.enqueue(project_id, None, "The bot was invited. Establish this channel's purpose from its name/topic. "
                           "Introduce yourself briefly and ask only if genuinely ambiguous. Do not assign research before a human request.",
                           key="onboarding:" + channel_id, payload={"channel": channel_id, "onboarding": True})
        return channel

    def receive(self, channel_id, timestamp, thread, user, text):
        channel = self.store.get("channels", channel_id)
        if not channel or not channel.get("active"):
            raise ValueError("Channel must be enrolled first")
        thread = thread or timestamp
        key = channel_id + ":" + timestamp
        with self.store.tx() as tx:
            if tx.get("messages", key):
                return
            project = tx.get("projects", channel["project"])
            project["human_started"] = True
            tx.put("projects", project)
            onboarding = tx.get("requests", "onboarding:" + channel_id)
            if onboarding and onboarding["status"] in ("queued", "running"):
                onboarding["status"] = "cancelled"
                tx.put("requests", onboarding)
            tx.put("messages", {"id": key, "project": project["id"], "channel": channel_id, "thread": thread,
                                "user": user, "text": text, "time": timestamp, "created": utc()})
            retired = tx.get("closed_threads", channel_id + ":" + thread)
            if retired:
                return
            tx.put("requests", {"id": key, "project": project["id"], "thread": thread,
                                "reason": "Human message: " + text[:2000], "priority": 100, "kind": "supervisor",
                                "payload": {"channel": channel_id}, "status": "queued", "created": time.time()})
            tx.event("human_message", project=project["id"], channel=channel_id, thread=thread)

    def workspace(self, task):
        return self.root / "workspaces" / task["project"] / task["id"]

    def context(self, session):
        project = self.store.get("projects", session["project"])
        tasks = [t for t in self.store.all("tasks") if t["project"] == project["id"]]
        task = self.store.get("tasks", session.get("task", ""))
        with self.store.tx() as tx:
            budget_usage = self.budget_usage(tx, project)
            tasks = [dict(t, launch_hold=self.task_launch_hold(tx, t)) for t in tasks]
            if task:
                task = dict(task, launch_hold=self.task_launch_hold(tx, task))
        messages = [m for m in self.store.all("messages") if m["project"] == project["id"]
                    and m["channel"] == session["channel"] and m["thread"] == session.get("thread")]
        from .research_story import story_context
        from .research_notes import metadata as notes_metadata
        with self.store.tx() as tx:
            from .research_notes import notebooks
            notes_list=notebooks(tx,project['id'])
            notes_info=notes_metadata(notes_list[0])
            notes_info['documents']=[]
            for note in notes_list:
                info=notes_metadata(note)
                info['permissions']=[{k:g.get(k) for k in ('id','base_revision','section','section_title')} for g in tx.all('research_note_grants') if g['project']==project['id'] and g.get('note_id',g['project'])==note['id'] and g['status']=='active' and g['base_revision']==note['revision']]
                notes_info['documents'].append(info)
            notes_info['permissions']=notes_info['documents'][0]['permissions']
            notes_info['instructions']='Always include note_id in notes_change. Read the selected document path first; titles may change.'

        data = {"research_notes": notes_info,"documentation_policy": self.cfg.get('documentation', {}),
                "report_workflow": 'prompts/report-workflow.md',
                "documentation_focus": self.store.get('documentation_focus', project['id'], {}).get('study_id'), "studies": [dict({k: s.get(k) for k in ('study_id','title','question','status_note','task_ids','verification_task','execution','verification','documentation','technical_record','source_record','report_pdf','paper_pdf','detailed_pdf','story_draft','known_gaps')},
                    publication_issues=s.get('checks',{}).get('issues',[]))
                    for s in self.store.all("studies") if s["project"] == project["id"] and
                    (session['role'] == 'supervisor' or (task and task.get('study_id') == s['study_id']))], "research_story": story_context(self, project["id"]), "project": project, "project_path": str(self.root / "projects" / project["folder"]),
                "role": session["role"], "task": task, "requests": session.get("reasons", []),
                "budget_usage": budget_usage,
                "activity": {
                    "model_sessions": [{k: s.get(k) for k in ("id", "task", "role", "status", "account", "started")}
                        for s in self.store.all("sessions") if s["project"] == project["id"] and s["status"] in ("running", "stopping")],
                    "experiments": [{k: j.get(k) for k in ("id", "task", "name", "status", "gpus", "gpu_mode", "gpu_memory_mib", "allocation", "started", "launch_hold")}
                        for j in self.store.all("jobs") if j["project"] == project["id"] and j["status"] in ("running", "stopping", "queued", "approval")],
                    "note": "An active specialist session means the agent is working, not that GPU experiments are running. Queued and approval-pending experiments have not started."},
                "resource_policy": "Prioritize rigorous completion of the authorized work. Allocate sufficient resources up front and grant justified increases within current limits; minimizing subscription usage is not the goal. Avoid redundant work. Experiment approvals and actual hardware/provider limits still apply.",
                "experiment_limits": {
                    "gpu_sharing": self.cfg['resources'].get('gpu_sharing', {}),
                    "max_gpus_per_assignment": 8,
                    "max_cpu_threads_per_assignment": self.cfg["resources"].get("max_cpu_threads_per_assignment", self.cfg["resources"]["max_cpu_threads_per_job"]),
                    "max_cpu_threads_per_job": self.cfg["resources"]["max_cpu_threads_per_job"],
                    "default_engineering_max_jobs": self.cfg["budgets"].get("task_jobs",4),
                    "max_running_jobs": self.cfg["budgets"]["max_jobs"],
                    "human_approval_above_job_hours": self.cfg["budgets"]["expensive_job_hours"],
                    "note": "The supervisor chooses per-assignment GPU, CPU, per-job duration, aggregate job-hours, and outstanding job grants sized to the deliverable. A job over the approval threshold still needs human approval."},
                "assignment_limits": {
                    "max_session_minutes": self.cfg["scheduling"]["max_session_minutes"],
                    "max_task_sessions": self.cfg["scheduling"].get("max_task_sessions", 30),
                    "max_task_children": self.cfg["scheduling"].get("max_task_children", 16),
                    "default_task_budget": {k: self.cfg["budgets"][k] for k in ("task_minutes", "task_sessions", "task_children")},
                    "note": "session_minutes must be positive and at most max_session_minutes. A longer aggregate budget spans multiple bounded sessions; never increase a single session beyond this limit."},
                "claude_account": session.get("account"),
                "session_history_directory": str(history.project_directory(self, session["project"])) if history.enabled(self.cfg) else None,
                "shared_memory_directory": str(customization.memory_directory(self, session["project"])) if self.cfg["claude"].get("shared_memory", True) else None,
                "history_parent": session.get("history_parent"),
                "subscription": subscription.view(self),
                "subscription_policy": self.cfg.get('subscription', {}),
                "native_subagents": dict(self.cfg.get('native_subagents', {}), available=delegation.enabled(self.cfg, session)),
                "scheduled_reviews": [w for w in self.store.all("wakeups") if w["project"] == project["id"] and w["status"] == "pending"],
                "recent_digests": [{k: d[k] for k in ('key', 'topic', 'period_start', 'period_end', 'summary', 'version')}
                    for d in sorted(self.store.all('digests'), key=lambda d: d['period_end'], reverse=True)
                    if d['project'] == project['id']][:12],
                "conversation": messages[-16:], "library": str(self.root / "library"),
                "checkpoint": self.read_checkpoint(session["workspace"]),
                "previous_result": str(Path(session["workspace"]) / "control" / "last-result.json"),
                "other_projects": [{"id": p["id"], "name": p["name"], "mode": p["mode"]} for p in self.store.all("projects") if p["id"] != project["id"]],
                "resume_from": [{"workspace": old["workspace"], "checkpoint": self.read_checkpoint(old["workspace"]), "error": old.get("error")}
                                for sid in session.get("resume_from", []) if (old := self.store.get("sessions", sid))],
                "tasks": [{k: t.get(k) for k in ("id", "study_id", "role", "goal", "status", "priority", "report", "notes", "verification_target", "requires_verification", "verdict", "acceptance", "summary", "usage_class", "subscription_hold", "not_before", "defer_reason", "launch_hold")}
                          for t in tasks if t["status"] != "cancelled"][-30:]}
        if task:
            # Explicit evidence sources survive the recent-task preview limit.
            study=self.store.get('studies',project['id']+':'+str(task.get('study_id')), {})
            peer_ids=set(study.get('task_ids',[]))|set(task.get('depends',[]))
            if study.get('verification_task'):peer_ids.add(study['verification_task'])
            listed={t['id'] for t in data['tasks']}
            data['tasks'] += [{k:t.get(k) for k in ('id','study_id','role','status','report','summary')}
                              for t in tasks if t['id'] in peer_ids and t['id'] not in listed]
            data["root_allocation"] = self.store.get("tasks", task["root_task"])
            data["jobs"] = [j for j in self.store.all("jobs") if j.get("task") == task["id"]]
            data["children"] = [t for t in tasks if t.get("parent") == task["id"]]
            data["meeting"] = self.store.get("meetings", task.get("meeting", ""))
        return data

    @staticmethod
    def read_checkpoint(workspace):
        path = Path(workspace) / "checkpoint.json"
        try:
            if path.stat().st_size > 100000:
                return {"error": "Checkpoint too large; inspect it on disk"}
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def budget_usage(self, tx, project):
        now = dt.datetime.now(ZoneInfo(self.cfg.get("timezone", "UTC")))
        daily = [s for s in tx.all("sessions")
                 if dt.datetime.fromtimestamp(s["created"], now.tzinfo).date() == now.date()]
        def cost(s):
            return s.get("seconds", s["minutes"] * 60 if s["status"] in ("running", "stopping") else 0) / 60
        project_used = sum(cost(s) for s in daily if s["project"] == project["id"])
        global_used = sum(cost(s) for s in daily)
        reset = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return {"accounting_date": str(now.date()), "timezone": str(now.tzinfo),
                "resets_at": reset.timestamp(),
                "project_used_minutes": project_used, "project_limit_minutes": project["daily_minutes"],
                "global_used_minutes": global_used, "global_limit_minutes": self.cfg["budgets"]["daily_agent_minutes"]}

    def task_launch_hold(self, tx, task):
        """Explain current deterministic launch gates without dispatching work."""
        if task["status"] not in ("queued", "deferred", "needs_resources", "blocked", "partial"):
            return None
        from .documentation_policy import launch_issue
        documentation_hold = launch_issue(self, tx, task)
        if documentation_hold:
            return {'reason': documentation_hold}
        now = time.time()
        project = tx.get("projects", task["project"])
        reason, retry_at = None, None
        if not accounts.profiles(self.cfg) and tx.get("settings", "authentication", {}).get("required"):
            reason = "Claude login needs refreshing"
        elif tx.get("settings", "pause", {}).get("paused"):
            reason = "platform paused"
        elif not accounts.profiles(self.cfg) and tx.get("settings", "cooldown", {}).get("until", 0) > now:
            reason = "provider cooldown"
            retry_at = tx.get("settings", "cooldown")["until"]
        elif project["status"] != "active":
            reason = "project is " + project["status"]
        elif not tx.get("channels", task["channel"], {}).get("active"):
            reason = "channel unavailable"
        elif tx.get("closed_threads", task["channel"] + ":" + str(task.get("thread"))):
            reason = "discussion closed"
        elif task["status"] == "deferred" and task.get("not_before", 0) > now:
            reason, retry_at = "assignment deferred", task["not_before"]
        elif task["status"] in ("needs_resources", "blocked", "partial"):
            reason = task.get("last_error") or "assignment needs supervisor authorization"
        elif any((tx.get("tasks", d) or {}).get("status") != "accepted" for d in task.get("depends", [])):
            reason = "waiting for accepted dependencies"
        elif (tx.get("tasks", task["root_task"]) or {}).get("status") == "cancelled":
            reason = "root assignment cancelled"
        else:
            quota = subscription.gate(self, task["model"], task.get("usage_class", "large"))
            if quota:
                reason, retry_at = quota["reason"], quota["retry_at"]
            else:
                reason = self.budget_available(tx, project, task["session_minutes"], task["root_task"])
                if reason in ("global daily session-time budget", "project daily session-time budget", "daily reported-output-token budget"):
                    retry_at = self.budget_usage(tx, project)["resets_at"]
        if not reason:
            active = [s for s in tx.all("sessions") if s["status"] in ("running", "stopping")]
            slots = self.cfg["scheduling"]["max_sessions"] - len(active)
            if slots <= 0:
                reason = "all model session slots occupied"
            elif (self.cfg["scheduling"].get("reserve_supervisor_slot", True)
                  and self.cfg["scheduling"]["max_sessions"] > 1 and slots <= 1
                  and not any(s["role"] == "supervisor" for s in active)):
                reason = "remaining session slot reserved for supervisor"
            elif task["role"] == "worker":
                if sum(s["role"] == "worker" for s in active) >= self.cfg["scheduling"]["max_workers"]:
                    reason = "worker concurrency limit"
            elif any(s["role"] == task["role"] for s in active):
                reason = "another " + task["role"] + " session is active"
        if reason:
            return {"reason": reason, "retry_at": retry_at,
                    "requested_minutes": task["session_minutes"], "budget_usage": self.budget_usage(tx, project)}
        return None

    def budget_available(self, tx, project, minutes, root_task=None):
        now = dt.datetime.now(ZoneInfo(self.cfg.get("timezone", "UTC")))
        sessions = tx.all("sessions")
        daily = [s for s in sessions if dt.datetime.fromtimestamp(s["created"], now.tzinfo).date() == now.date()]
        def cost(s):
            return s.get("seconds", s["minutes"] * 60 if s["status"] in ("running", "stopping") else 0) / 60
        limits = self.cfg["budgets"]
        if sum(cost(s) for s in daily) + minutes > limits["daily_agent_minutes"]:
            return "global daily session-time budget"
        if sum(cost(s) for s in daily if s["project"] == project["id"]) + minutes > project["daily_minutes"]:
            return "project daily session-time budget"
        token_limit = limits.get("daily_output_tokens")
        if token_limit and sum(s.get("usage", {}).get("output_tokens", 0) or 0 for s in daily) >= token_limit:
            return "daily reported-output-token budget"
        if root_task:
            root = tx.get("tasks", root_task)
            group = [s for s in sessions if s.get("root_task") == root_task]
            charged = [s for s in group if s["status"] not in ("quota", "auth") or any((s.get("usage", {}).get(k) or 0) > 0
                       for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))]
            if len(charged) >= root["budget"]["sessions"]:
                return "assignment session limit"
            if sum(cost(s) for s in group) + minutes > root["budget"]["minutes"]:
                return "assignment aggregate session-time budget"
        return None

    def create_task(self, tx, session, action, *, parent=None):
        project = tx.get("projects", session["project"])
        if project["status"] != "active" or not project["human_started"]:
            raise ValueError("No worker assignments before a human starts the project, or while it is paused")
        role = action.get("role")
        if role not in ("engineer", "literature", "verifier", "worker"):
            raise ValueError("Unknown specialist role")
        if parent and role != "worker":
            raise ValueError("Specialists delegate managed workers, not other specialist heads")
        if not parent and role == "worker":
            raise ValueError("Workers require a parent specialist assignment")
        model, effort = profile(action["model"], action["effort"])
        goal, acceptance = action.get("goal", "").strip(), action.get("acceptance", "").strip()
        if not goal or not acceptance:
            raise ValueError("A work order needs a goal and acceptance criteria")
        from .documentation_policy import is_documentation
        doc_work = self.cfg.get('documentation', {}).get('enabled', False) and is_documentation(action)
        default_minutes = self.cfg['scheduling']['max_session_minutes'] if doc_work else 10
        minutes = float(action.get("session_minutes", default_minutes))
        if not 0 < minutes <= self.cfg["scheduling"]["max_session_minutes"]:
            raise ValueError(f"Requested session_minutes={minutes:g}; must be greater than 0 and at most {self.cfg['scheduling']['max_session_minutes']:g}. Use multiple bounded sessions within the aggregate task budget.")
        key = action.get("key") or goal.lower().strip()
        for existing in tx.all("tasks"):
            if existing["project"] == project["id"] and existing["key"] == key and existing["status"] != "cancelled":
                return existing
        defaults = self.cfg["budgets"]
        budget = {"minutes": defaults["task_minutes"], "sessions": defaults["task_sessions"],
                  "children": defaults["task_children"]}
        if doc_work:
            budget.update(children=0)
        if parent:
            if parent.get("parent"):
                raise ValueError("Workers cannot recursively hire more workers")
            root = tx.get("tasks", parent["root_task"])
            children = [t for t in tx.all("tasks") if t.get("root_task") == root["id"] and t.get("parent")]
            if len(children) >= root["budget"]["children"]:
                raise ValueError("Assignment child-worker allowance exhausted; request an escalation")
            if minutes > parent["session_minutes"]:
                raise ValueError("Child session duration cannot exceed its specialist's allocation")
            # A specialist cannot upgrade model/effort beyond its supervisor-approved ceiling.
            ranks = {"haiku": 0, "sonnet": 1, "opus": 2}
            efforts = {"low": 0, "medium": 1, "high": 2, "xhigh": 3}
            if ranks[model] > ranks[parent["model"]] or efforts[effort] > efforts[parent["effort"]]:
                raise ValueError("Child profile exceeds the specialist allocation; ask the supervisor")
            budget = root["budget"]
        else:
            budget.update(action.get("budget", {}))
            budget.pop('output_tokens',None)  # Ignore legacy allowances; usage is tracked, not task-capped.
            if set(budget) != {"minutes", "sessions", "children"} or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in budget.values()):
                raise ValueError("Invalid assignment budget")
            if budget["minutes"] > project["daily_minutes"] or budget["sessions"] > self.cfg["scheduling"].get("max_task_sessions", 30) or budget["children"] > self.cfg["scheduling"].get("max_task_children", 16):
                raise ValueError("Assignment budget exceeds configured project/session/worker bounds")
            if budget["minutes"] < minutes or budget["sessions"] < 1:
                raise ValueError("Assignment budget must permit at least its initial session")
        deps = action.get("depends", [])
        if any(not tx.get("tasks", d) or tx.get("tasks", d)["project"] != project["id"] for d in deps):
            raise ValueError("Dependencies must be tasks in this project")
        if action.get("verification_target"):
            target = tx.get("tasks", action["verification_target"])
            if role != "verifier" or not target or target["project"] != project["id"] or target["status"] != "awaiting_review":
                raise ValueError("Verification must target a submitted task in this project")
        study_id = action.get('study_id') or (parent or {}).get('study_id')
        if study_id and not tx.get('studies', project['id']+':'+study_id):
            raise ValueError('Register this study before assigning its tasks')
        if doc_work and study_id:
            focus = tx.get('documentation_focus', project['id'], {})
            cycle = focus.get('cycle') if focus.get('study_id') == study_id else None
            # The stable work-order key above handles retries. A phase/PDF is not
            # a task identity: a later correction may legitimately share it.
        else:
            cycle = None
        task_id = uid()
        resources = {"max_gpus": 1 if role == "engineer" else 0,
                     "max_cpu_threads": self.cfg["resources"].get("max_cpu_threads_per_assignment",self.cfg["resources"]["max_cpu_threads_per_job"]) if role == "engineer" else min(4, self.cfg["resources"]["max_cpu_threads_per_job"]),
                     "max_job_hours": 24 if role == "engineer" else 1,
                     "total_job_hours": 4 if role == "engineer" else .5, "max_jobs": self.cfg["budgets"].get("task_jobs",4) if role == "engineer" else 1}
        if doc_work:
            resources.update(max_gpus=0,max_cpu_threads=min(4,self.cfg['resources']['max_cpu_threads_per_job']),
                             max_job_hours=self.cfg['documentation'].get('max_build_minutes',10)/60,total_job_hours=.5,max_jobs=1)
        resources.update(action.get("resources", {}))
        if parent:
            resources = tx.get("tasks", parent["root_task"])["resources"]
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in resources.values()):
            raise ValueError("Resource bounds must be nonnegative numbers")
        if resources["max_cpu_threads"] > self.cfg["resources"].get("max_cpu_threads_per_assignment",self.cfg["resources"]["max_cpu_threads_per_job"]) or resources["max_gpus"] > 8:
            raise ValueError("Resource request exceeds platform ceilings")
        task = {"id": task_id, "study_id": study_id,
                "work_kind": action.get('work_kind') or (parent or {}).get('work_kind'),
                "document_phase": action.get('document_phase') or (parent or {}).get('document_phase'),
                "document_kind": action.get('document_kind') or (parent or {}).get('document_kind'),
                "document_cycle": cycle, "key": key, "project": project["id"], "channel": session["channel"],
                "thread": session.get("thread"), "role": role, "goal": goal, "acceptance": acceptance,
                "model": model, "effort": effort, "session_minutes": minutes,
                "priority": min(parent["priority"] if parent else 100, max(0, int(action.get("priority", 50)))), "budget": budget,
                "resources": resources,
                "status": "queued", "created": time.time(), "parent": parent["id"] if parent else None,
                "root_task": parent["root_task"] if parent else task_id, "depends": deps,
                "requires_verification": bool(action.get("requires_verification", role == "engineer")),
                "verification_target": action.get("verification_target"),
                "allow_job_followup": bool(action.get("allow_job_followup", True)),
                "allow_child_followup": bool(action.get("allow_child_followup", True)),
                "direct_reply": bool(action.get("direct_reply", False)) if not parent else False,
                "usage_class": action.get("usage_class", "large" if model == "opus" or effort == "xhigh" or minutes > 5 else "small"),
                "notes": [], "attempts": 0}
        if task['usage_class'] not in ('small', 'large'):
            raise ValueError('usage_class must be small or large')
        if action.get('not_before'):
            task.update(not_before=wakeups.when({'at': action['not_before']}), status='deferred')
        from .documentation_policy import profile_issue
        issue = profile_issue(self.cfg, task)
        if issue:
            task['launch_hold'] = {'reason': issue}
        tx.put("tasks", task)
        tx.event("work_authorized", task=task_id, parent=task["parent"], role=role, model=model, effort=effort, budget=budget)
        return task

    def notify_supervisor(self, tx, task, reason, *, key=None):
        if task["status"] == "cancelled" or tx.get("closed_threads", task["channel"] + ":" + str(task["thread"])):
            return
        request_id = key or uid()
        if tx.get("requests", request_id):
            return
        tx.put("requests", {"id": request_id, "project": task["project"], "thread": task["thread"],
                            "reason": reason, "priority": max(60, task["priority"]), "kind": "supervisor",
                            "payload": {"channel": task["channel"], "task": task["id"]},
                            "status": "queued", "created": time.time()})

    def _freeze_evidence(self, session, result):
        project = self.store.get("projects", session["project"])
        destination = self.root / "projects" / project["folder"] / "reports" / session["id"]
        destination.mkdir(parents=True, exist_ok=True)
        manifest = []
        for evidence in result.get("evidence", []):
            path = scoped(session["workspace"], evidence["path"])
            if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError("Evidence must name individual files under 20 MiB; link large run outputs in the report")
            relative = path.relative_to(Path(session["workspace"]).resolve())
            target = destination / "artifacts" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            manifest.append({"path": str(target), "sha256": digest(target), "description": evidence["description"]})
        report = dict(result, session=session["id"], task=session.get("task"), role=session["role"],
                      model=session["model"], effort=session["effort"], created=utc(), evidence=manifest)
        atomic_json(destination / "report.json", report)
        return str(destination / "report.json"), manifest

    def apply_result(self, session_id, result, usage=None):
        session = self.store.get("sessions", session_id)
        if session.get("applied"):
            return
        required = {"reply", "status", "summary", "findings", "limitations", "evidence", "next_steps", "actions"}
        if not required.issubset(result) or not isinstance(result["actions"], list) or len(result["actions"]) > 16:
            raise ValueError("Incomplete structured result or too many actions")
        report, manifest = self._freeze_evidence(session, result)
        with self.store.tx() as tx:
            session = tx.get("sessions", session_id)
            if session.get("applied"):
                return
            task = tx.get("tasks", session.get("task", ""))
            from .document_packages import register
            register(tx, self.root, session, manifest)
            for action in result["actions"]:
                self._action(tx, session, task, action)
            if task:
                task = tx.get("tasks", task["id"])
                jobs = [j for j in tx.all("jobs") if j.get("task") == task["id"] and j["status"] in ACTIVE_JOBS]
                children = [t for t in tx.all("tasks") if t.get("parent") == task["id"] and t["status"] not in TERMINAL_TASKS]
                state = result["status"]
                if jobs:
                    state = "waiting_jobs"
                elif children:
                    state = "waiting_children"
                elif state in ("waiting_jobs", "waiting_children"):
                    state = "blocked"  # Never poll an empty dependency indefinitely.
                task.update(status="awaiting_review" if state == "completed" else state,
                            report=report, evidence=manifest, summary=result["summary"],
                            next_steps=result["next_steps"], verdict=result.get("verdict"), updated=time.time())
                if task.get("parent"):
                    task["status"] = "accepted" if state == "completed" else state
                if task.get("direct_reply") and not task["requires_verification"] and state == "completed":
                    task["status"] = "accepted"
                if task.get("meeting") and state == "completed":
                    task["status"] = "accepted"
                tx.put("tasks", task)
                if task.get("meeting") and state == "completed":
                    meeting = tx.get("meetings", task["meeting"])
                    meeting["reports"].append(report)
                    if meeting["turn"] < meeting["max_turns"] and not result.get("resolved", False):
                        self.meeting_turn(tx, session, meeting)
                    else:
                        meeting["status"] = "done"
                        tx.put("meetings", meeting)
                        origin = tx.get("tasks", meeting["origin"])
                        origin["notes"].append({"type": "meeting", "reports": meeting["reports"], "time": utc()})
                        origin["status"] = "queued"
                        tx.put("tasks", origin)
                        self.notify_supervisor(tx, origin, "The bounded specialist clarification finished; its reports are saved.")
                if state not in ("waiting_jobs", "waiting_children"):
                    if task.get("parent"):
                        self._resume_parent(tx, task)
                    elif not task.get("meeting") and not task.get("direct_reply"):
                        self.notify_supervisor(tx, task, "Specialist returned a report: " + result["summary"], key="result:" + session_id)
            reply = result["reply"].strip()
            if reply and (session["role"] == "supervisor" or task and task.get("direct_reply")):
                if session["role"] != "supervisor":
                    reply = session["role"].replace("literature", "Literature researcher").capitalize() + ":\n" + reply
                key = "reply:" + session_id
                tx.put("outbox", {"id": key, "channel": session["channel"], "thread": session.get("thread"),
                                  "text": reply, "urgent": bool(result.get("urgent") and result.get("urgent_reason")),
                                  "status": "queued", "created": time.time()})
            session.update(applied=True, status="done", report=report, usage=usage or {}, finished=time.time())
            tx.put("sessions", session)
            for request_id in session.get("request_ids", []):
                req = tx.get("requests", request_id)
                req["status"] = "done"
                tx.put("requests", req)
            tx.event("result_applied", session=session_id, task=session.get("task"), report=report)
        self.refresh_views(session["project"])

    def _resume_parent(self, tx, child):
        parent = tx.get("tasks", child["parent"])
        siblings = [t for t in tx.all("tasks") if t.get("parent") == parent["id"]]
        if all(t["status"] == "accepted" for t in siblings) and parent["status"] == "waiting_children" and parent["allow_child_followup"]:
            parent["status"] = "queued"
            tx.put("tasks", parent)
        elif child["status"] != "accepted":
            self.notify_supervisor(tx, parent, "A delegated worker is blocked; inspect its report before scheduling more work.")

    def _owned_task(self, tx, session, task_id):
        task = tx.get("tasks", task_id)
        if not task or task["project"] != session["project"]:
            raise ValueError("Task is outside this project")
        return task

    def _action(self, tx, session, task, action):
        kind = action.get("type")
        supervisor = session["role"] == "supervisor"
        if kind in ('propose_research_notes','edit_research_notes'):
            from . import research_notes
            if session['role'] not in ('supervisor','engineer','literature','verifier'):
                raise ValueError('Only project specialists or the supervisor may propose notes changes')
            if kind=='edit_research_notes' and not supervisor:
                raise ValueError('Only the supervisor may use a human notes-edit permission')
            proposal=research_notes.propose(self.root,tx,session,action.get('notes_change',{}))
            if kind=='edit_research_notes':
                research_notes.accept(tx,proposal['note_id'],proposal['id'],proposal['base_revision'],author='supervisor: authorized edit',grant_id=action.get('grant_id') or 'missing')
            return
        if kind == 'publish_digest' and supervisor:
            from .digests import publish
            publish(tx, session, action)
            return
        if kind == "assign" and supervisor:
            self.create_task(tx, session, action)
        elif kind == 'set_documentation_focus' and supervisor:
            from .documentation_policy import select_focus
            select_focus(tx, session['project'], action['study_id'], new_cycle=action.get('new_cycle', False), root=self.root)
        elif kind == 'update_study' and supervisor:
            from .studies import update
            update(self, tx, session, action)
        elif kind == 'schedule_wake' and supervisor:
            wakeups.schedule(tx, session, action)
        elif kind == 'cancel_wake' and supervisor:
            wake = tx.get('wakeups', action['wake_id'])
            if not wake or wake['project'] != session['project']:
                raise ValueError('Wake-up is outside this project')
            wake['status'] = 'cancelled'
            tx.put('wakeups', wake)
            pending = tx.get('requests', wake.get('last_request_id', 'wake:' + wake['id']))
            if pending and pending['status'] == 'queued':
                pending['status'] = 'cancelled'
                tx.put('requests', pending)
        elif kind == 'defer_task' and supervisor:
            target = self._owned_task(tx, session, action['task'])
            if target['status'] not in ('queued', 'deferred', 'partial', 'blocked', 'needs_resources'):
                raise ValueError('Only unstarted or stopped work can be deferred; wrap up active work first')
            target.update(status='deferred', not_before=wakeups.when(action), defer_reason=action.get('reason', 'Scheduled for later'))
            tx.put('tasks', target)
        elif kind == "spawn" and task and session["role"] in ("engineer", "literature"):
            if delegation.enabled(self.cfg, session):
                raise ValueError('Use native CLI helpers within this assignment, not managed spawn actions. Ask the supervisor for a separate durable assignment when needed.')
            self.create_task(tx, session, dict(action, role="worker"), parent=task)
        elif kind in ("clarification", "escalate") and task:
            note = {"type": kind, "question": action.get("question", ""), "request": action.get("request", {}), "time": utc()}
            if not note["question"]:
                raise ValueError("Clarification/escalation needs a concrete question")
            task["notes"].append(note)
            tx.put("tasks", task)
            self.notify_supervisor(tx, task, kind.capitalize() + ": " + note["question"])
        elif kind == "set_project" and supervisor:
            project = tx.get("projects", session["project"])
            mode = action.get("mode", project["mode"])
            if mode not in ("research", "reading", "ideas"):
                raise ValueError("Project mode must be research, reading, or ideas")
            for field in ("purpose", "brief", "name"):
                if field in action:
                    project[field] = str(action[field])
            project["mode"] = mode
            tx.put("projects", project)
        elif kind == "link_channel" and supervisor:
            target = tx.get("projects", action["project"])
            if not target:
                raise ValueError("Unknown project")
            channel = tx.get("channels", session["channel"])
            # Cannot silently move active work between projects.
            if any(t["project"] == channel["project"] and t["status"] not in TERMINAL_TASKS for t in tx.all("tasks")):
                raise ValueError("Resolve this channel's active assignments before linking it to another project")
            channel["project"] = target["id"]
            tx.put("channels", channel)
            for req in tx.all("requests"):
                if req["project"] == session["project"] and req["payload"].get("channel") == session["channel"] and req["status"] == "queued":
                    req["status"] = "cancelled"
                    tx.put("requests", req)
            for message in tx.all("messages"):
                if message["channel"] == session["channel"]:
                    message["project"] = target["id"]
                    tx.put("messages", message)
            linked = set(target.get("linked_projects", []))
            linked.add(session["project"])
            target["linked_projects"] = sorted(linked)
            tx.put("projects", target)
            if not any(c["project"] == session["project"] and c["active"] for c in tx.all("channels")):
                old = tx.get("projects", session["project"])
                old["status"] = "paused"
                tx.put("projects", old)
        elif kind == "decision" and supervisor:
            decision = {"id": uid(), "project": session["project"], "thread": session.get("thread"),
                        "text": action["text"], "sources": action.get("sources", []),
                        "disagreement": action.get("disagreement", ""), "time": utc()}
            tx.put("decisions", decision)
        elif kind in ("write_note", "make_folder") and supervisor:
            project = tx.get("projects", session["project"])
            base = self.root / "projects" / project["folder"]
            path = scoped(base, action["path"], exists=False)
            relative = path.relative_to(base).as_posix()
            from .research_story import table_mode,PATH
            if relative.startswith(('notes/research-notes','notes/research-note-proposals')):
                raise ValueError('Research Notes are human-owned. Use proposal actions or a matching one-time edit permission.')
            if relative==PATH and table_mode(self):
                raise ValueError('Research story is a generated study table. Use update_study to change its rows.')
            if not relative.startswith(("notes/", "experiments/")) or relative in ("notes/tasks.json", "notes/decisions.json", "notes/papers.json", "notes/studies.json") or relative.startswith(("notes/conversations/", "notes/history/")):
                raise ValueError("Supervisor notes/folders belong in notes/ or experiments/, outside generated records")
            if kind == "write_note" and len(action.get("text", "")) > 100000:
                raise ValueError("Keep shared notes compact; link large artifacts")
            note = {"id": session["project"] + ":" + relative, "project": session["project"], "path": relative,
                    "text": action.get("text", ""), "folder": kind == "make_folder", "time": utc()}
            tx.put("project_notes", note)
            tx.put("note_history", note, key=uid())
        elif kind in ("reprioritize", "grant", "answer", "cancel", "accept", "reject") and supervisor:
            target = self._owned_task(tx, session, action["task"])
            if kind == "reprioritize":
                target["priority"] = max(0, min(100, int(action["priority"])))
                target["preempt_reason"] = action.get("reason", "")
            elif kind == "grant":
                root = tx.get("tasks", target["root_task"])
                allowed = {"minutes", "sessions", "children", "output_tokens"}
                if not set(action.get("budget", {})).issubset(allowed):
                    raise ValueError("Unknown budget field")
                new = dict(root["budget"], **action.get("budget", {}))
                new.pop('output_tokens',None)
                if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in new.values()):
                    raise ValueError("Invalid budget")
                if new["minutes"] > tx.get("projects", session["project"])["daily_minutes"] or new["children"] > self.cfg["scheduling"].get("max_task_children", 16) or new["sessions"] > self.cfg["scheduling"].get("max_task_sessions", 30):
                    raise ValueError("Budget grant exceeds project bounds")
                root["budget"] = new
                if "model" in action or "effort" in action:
                    target["model"], target["effort"] = profile(action.get("model", target["model"]), action.get("effort", target["effort"]))
                if "session_minutes" in action:
                    minutes = float(action["session_minutes"])
                    if not 0 < minutes <= self.cfg["scheduling"]["max_session_minutes"] or minutes > new["minutes"]:
                        raise ValueError("Session duration exceeds the granted/platform limit")
                    target["session_minutes"] = minutes
                if "resources" in action:
                    proposed = dict(root["resources"], **action["resources"])
                    if any(not isinstance(v, (int, float)) or v < 0 for v in proposed.values()) or proposed["max_gpus"] > 8 or proposed["max_cpu_threads"] > self.cfg["resources"].get("max_cpu_threads_per_assignment",self.cfg["resources"]["max_cpu_threads_per_job"]):
                        raise ValueError("Resource grant exceeds platform ceilings")
                    root["resources"] = proposed
                tx.put("tasks", root)
                if root["id"] == target["id"]:
                    target["budget"] = new
                    target["resources"] = root["resources"]
                target["status"] = "running" if any(s.get("task") == target["id"] and s["status"] in ("running", "stopping") for s in tx.all("sessions")) else "queued"
            elif kind == "answer":
                target["notes"].append({"type": "answer", "text": action["text"], "time": utc()})
                target["status"] = "running" if any(s.get("task") == target["id"] and s["status"] in ("running", "stopping") for s in tx.all("sessions")) else "queued"
            elif kind == "cancel":
                target["status"] = "cancelled"
                for child in tx.all("tasks"):
                    if child.get("root_task") == target["id"] and child["id"] != target["id"]:
                        child["status"] = "cancelled"
                        tx.put("tasks", child)
                for job in tx.all("jobs"):
                    if job.get("root_task") == target["root_task"] and job["status"] in ("queued", "approval"):
                        job["status"] = "cancelled"
                        tx.put("jobs", job)
            elif kind == "accept":
                if target["status"] != "awaiting_review" or not target.get("evidence"):
                    raise ValueError("Acceptance requires a submitted report with evidence")
                if target["requires_verification"]:
                    checks = [t for t in tx.all("tasks") if t.get("verification_target") == target["id"]
                              and t["status"] == "accepted" and t.get("verdict") == "pass" and t.get("evidence")]
                    if not checks:
                        raise ValueError("Independent passing verification is required before accepting this result")
                target["status"] = "accepted"
            elif kind == "reject":
                target["status"] = "blocked"
                target["notes"].append({"type": "review", "text": action["reason"], "time": utc()})
            tx.put("tasks", target)
        elif kind == "meeting" and supervisor:
            self.create_meeting(tx, session, action)
        elif kind == "publish_paper" and task and session["role"] in ("literature", "worker"):
            self.publish_paper(tx, session, action)
        elif kind == "pause_project" and supervisor:
            project = tx.get("projects", session["project"])
            project["status"] = "paused" if action.get("paused", True) else "active"
            tx.put("projects", project)
        elif kind == "share_file" and supervisor:
            project = tx.get("projects", session["project"])
            base = self.root / "projects" / project["folder"]
            path = scoped(base, action["path"])
            if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError("Share an individual project artifact under 20 MiB")
            key = "file:" + session["id"] + ":" + str(len(tx.all("outbox")))
            tx.put("outbox", {"id": key, "channel": session["channel"], "thread": session.get("thread"),
                              "file": str(path), "title": action.get("title", "Research evidence"),
                              "status": "queued", "created": time.time()})
        elif kind == "promote_code" and supervisor:
            target = self._owned_task(tx, session, action["task"])
            if target["status"] != "accepted":
                raise ValueError("Only accepted code can be promoted for reuse")
            selected = set(action.get("paths", []))
            evidence = [e for e in target.get("evidence", []) if not selected or e["path"] in selected]
            if not evidence:
                raise ValueError("Select accepted, frozen source/test/config evidence")
            for item in evidence:
                if digest(item["path"]) != item["sha256"]:
                    raise ValueError("Frozen evidence changed after review; cannot promote")
            tx.put("code_revisions", {"id": target["id"], "project": session["project"], "files": evidence, "time": utc()})
        elif kind == "close_thread" and supervisor:
            key = session["channel"] + ":" + str(session.get("thread"))
            tx.put("closed_threads", {"id": key, "time": utc()})
            for other in tx.all("tasks"):
                if other["project"] == session["project"] and other["thread"] == session.get("thread") and other["status"] not in TERMINAL_TASKS:
                    other["status"] = "cancelled"
                    tx.put("tasks", other)
        else:
            raise ValueError(f"Action {kind!r} is not authorized for {session['role']}")
        tx.event("action", session=session["id"], task=session.get("task"), type=kind)

    def publish_paper(self, tx, session, action):
        source = scoped(session["workspace"], action["path"])
        note = json.loads(source.read_text())
        fields = {"canonical_id", "title", "authors", "year", "url", "read_depth", "sections_read", "method", "results", "limitations", "supporting_sections"}
        if not fields.issubset(note) or note["read_depth"] not in ("abstract", "partial", "full"):
            raise ValueError("Paper record is missing required evidence/reading-depth fields")
        if not str(note["url"]).startswith("https://"):
            raise ValueError("Paper notes require a source URL")
        key = note["canonical_id"].strip().lower()
        prior = tx.get("papers", key, {})
        ranks = {"abstract": 0, "partial": 1, "full": 2}
        if prior and ranks[note["read_depth"]] < ranks[prior["read_depth"]]:
            raise ValueError("Do not downgrade shared reading depth; publish a supplemental note instead")
        version = prior.get("version", 0) + 1
        note.update(version=version, checked_by=session["role"], task=session["task"], updated=utc())
        tx.put("papers", note, key=key)
        tx.put("paper_versions", note, key=key + ":" + str(version))
        tx.put("paper_links", {"id": session["project"] + ":" + key, "project": session["project"],
                               "paper": key, "relevance": action.get("relevance", "")})

    def create_meeting(self, tx, session, action):
        origin = self._owned_task(tx, session, action["task"])
        turns = min(4, max(2, int(action.get("turns", 2))))
        meeting_id = uid()
        meeting = {"id": meeting_id, "project": session["project"], "origin": origin["id"],
                   "question": action["question"], "max_turns": turns, "turn": 0, "status": "active", "reports": [],
                   "model": action["model"], "effort": action["effort"], "session_minutes": min(5, action.get("session_minutes", 5))}
        tx.put("meetings", meeting)
        origin["status"] = "blocked"
        tx.put("tasks", origin)
        self.meeting_turn(tx, session, meeting)

    def meeting_turn(self, tx, session, meeting):
        role = "literature" if meeting["turn"] % 2 == 0 else "engineer"
        task = self.create_task(tx, session, {"role": role, "goal": "Resolve this bounded clarification: " + meeting["question"],
                 "acceptance": "Answer with source/code evidence and state remaining ambiguity", "model": meeting["model"],
                 "effort": meeting["effort"], "session_minutes": meeting["session_minutes"],
                 "budget": {"minutes": meeting["max_turns"] * meeting["session_minutes"], "sessions": meeting["max_turns"], "children": 0},
                 "key": "meeting:" + meeting["id"] + ":" + str(meeting["turn"]), "requires_verification": False})
        task["meeting"] = meeting["id"]
        if meeting.get("root_task"):
            task["root_task"] = meeting["root_task"]
        else:
            meeting["root_task"] = task["id"]
        tx.put("tasks", task)
        meeting["turn"] += 1
        tx.put("meetings", meeting)

    def refresh_views(self, project_id):
        from .research_notes import export as export_notes
        from .research_notes import notebooks
        with self.store.tx() as tx:note_ids=[n['id'] for n in notebooks(tx,project_id)]
        for note_id in note_ids:export_notes(self.root,self.store,note_id)
        project = self.store.get("projects", project_id)
        base = project_layout(self.root, project)
        for digest in self.store.all('digests'):
            if digest['project'] == project_id:
                path = scoped(base, f"notes/digests/{digest['key']}/{digest['period_end']}.md", exists=False)
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix('.tmp')
                from .digests import markdown
                temp.write_text(f"# {digest['title']}\n\n{digest['topic']} · {digest['period_start']} – {digest['period_end']} · Revision {digest['version']}\n\n{digest['summary']}\n\n{markdown(digest)}\n")
                temp.replace(path)
        tasks = [t for t in self.store.all("tasks") if t["project"] == project_id]
        atomic_json(base / "notes" / "tasks.json", tasks)
        atomic_json(base / "notes" / "studies.json", [s for s in self.store.all("studies") if s["project"] == project_id])
        atomic_json(base / "notes" / "decisions.json", [d for d in self.store.all("decisions") if d["project"] == project_id])
        atomic_json(base / "notes" / "papers.json", [p for p in self.store.all("paper_links") if p["project"] == project_id])
        for note in self.store.all("project_notes"):
            if note["project"] == project_id:
                path = scoped(base, note["path"], exists=False)
                if note.get("folder",False):
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temp = path.with_name(path.name + ".tmp")
                    temp.write_text(note["text"])
                    temp.replace(path)
        for note in self.store.all("papers"):
            folder = self.root / "library" / "papers" / paper_folder(note["canonical_id"])
            atomic_json(folder / f"v{note['version']}.json", note)
            atomic_json(folder / "note.json", note)
        for note in self.store.all("paper_versions"):
            atomic_json(self.root / "library" / "papers" / paper_folder(note["canonical_id"]) / f"v{note['version']}.json", note)
        for revision in self.store.all("code_revisions"):
            if revision["project"] == project_id:
                destination = base / "code" / "revisions" / revision["id"]
                destination.mkdir(parents=True, exist_ok=True)
                for index, item in enumerate(revision["files"]):
                    source = Path(item["path"])
                    # Keep original relative hierarchy beneath artifacts, avoiding basename collisions.
                    relative = str(source).split("/artifacts/", 1)[-1]
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        shutil.copy2(source, target)
                atomic_json(base / "code" / "current.json", revision)
