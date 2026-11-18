"""Offline tests; isolated data, fake Claude/Slack, and real subprocess/sandbox checks."""
import copy
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labcore.config import load, profile
from labcore.engine import Engine
from labcore.files import atomic_json
from labcore.runner import reset_time, parse_output
from labcore.runtime import Runtime
from labcore.slack import SlackBridge
from labcore.store import uid

SOURCE = Path(__file__).resolve().parents[1]


def result(**overrides):
    value = {"reply": "", "status": "completed", "summary": "Done", "findings": [], "limitations": [],
             "evidence": [], "next_steps": [], "actions": []}
    value.update(overrides)
    return value


class FakeSlack:
    token = "fake-not-a-token"
    def __init__(self):
        self.messages, self.files, self.counter = [], [], 0
        self.reactions = []
    def reactions_add(self, **kwargs):
        self.reactions.append(dict(action='add', **kwargs))
        return {'ok': True}
    def reactions_remove(self, **kwargs):
        self.reactions.append(dict(action='remove', **kwargs))
        return {'ok': True}
    def auth_test(self):
        return {"user_id": "BOT", "ok": True}
    def conversations_info(self, channel):
        return {"channel": {"id": channel, "name": "research-" + channel, "topic": {"value": "Dynamics research"}}}
    def users_conversations(self, **kwargs):
        return {"channels": [], "response_metadata": {}}
    def chat_postMessage(self, **kwargs):
        self.counter += 1
        record = dict(kwargs, ts=f"200.{self.counter}")
        self.messages.append(record)
        return record
    def files_upload_v2(self, **kwargs):
        self.files.append(kwargs)
        return {"ok": True}


class PlatformTests(unittest.TestCase):
    def test_legacy_task_output_allowance_never_blocks_but_usage_is_preserved(self):
        task=self.task()
        session=self.session('engineer',task)
        with self.engine.store.tx() as tx:
            task['budget']['output_tokens']=1;tx.put('tasks',task)
            session.update(status='done',seconds=1,usage={'output_tokens':4000000});tx.put('sessions',session)
            project=tx.get('projects',task['project'])
            self.assertIsNone(self.engine.budget_available(tx,project,task['session_minutes'],task['id']))
        self.assertEqual(self.engine.store.get('sessions',session['id'])['usage']['output_tokens'],4000000)

    def test_removed_backend_selector_is_rejected_instead_of_bypassing_claude(self):
        import yaml
        (self.root/'config.yaml').write_text(yaml.safe_dump(dict(self.cfg,agent_backend={'provider':'unsupported'})))
        with self.assertRaisesRegex(ValueError,'Only the Claude backend'):
            load(self.root)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("labcore", "prompts", "tests"):
            shutil.copytree(SOURCE / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(SOURCE / "lab.py", self.root / "lab.py")
        self.cfg = load()
        self.cfg['research_story'] = {'format':'narrative'}
        self.cfg["root"] = str(self.root)
        self.cfg["claude"]["accounts"] = []  # isolate single-account legacy fixtures
        self.cfg["claude"].update(bin=str(self.root / "tests/fake_claude.py"), python_env=None,
                                   supervisor_minutes=.1, sandbox=True)
        self.cfg["scheduling"].update(debounce_seconds=0, poll_seconds=.01, preemption_grace_seconds=.1)
        self.cfg['subscription'] = {'enabled': False}
        self.cfg['server_status'] = {'enabled': False}  # hardware telemetry has isolated tests
        self.cfg['native_subagents'] = {'enabled': False} # exercise legacy queued-worker migration paths
        self.cfg['documentation'] = {'enabled': False} # hard publication controls have isolated tests
        self.engine = Engine(self.cfg)
        self.runtime = Runtime(self.engine)
        self.client = FakeSlack()
        self.bridge = SlackBridge(self.engine, self.runtime, self.client)
        self.channel = self.engine.enroll("C1", "hidden-dynamics")
        # Most unit tests are about research, not the separate onboarding response.
        with self.engine.store.tx() as tx:
            onboarding = tx.get("requests", "onboarding:C1")
            onboarding["status"] = "done"
            tx.put("requests", onboarding)
        self.engine.receive("C1", "100.1", "100.1", "human", "Investigate this experiment")
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for session in self.engine.store.all("sessions"):
            self.runtime.runner.terminate(session, hard=True)
        for proc in list(self.runtime.runner.processes.values()) + list(self.runtime.jobs.processes.values()):
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def session(self, role="supervisor", task=None):
        sid = uid()
        session = self.runtime._session(sid, self.channel["project"], "C1", "100.1", role,
                                        "sonnet", "medium", .1, 50, self.root / "workspaces" / self.channel["project"] / sid)
        if task:
            session.update(task=task["id"], root_task=task["root_task"], workspace=str(self.engine.workspace(task)))
        Path(session["workspace"]).mkdir(parents=True, exist_ok=True)
        with self.engine.store.tx() as tx:
            tx.put("sessions", session)
        return session

    def task(self, **overrides):
        session = {"id": uid(), "project": self.channel["project"], "channel": "C1", "thread": "100.1", "role": "supervisor"}
        action = {"role": "engineer", "goal": "Implement baseline", "acceptance": "Sanity tests pass", "model": "sonnet",
                  "effort": "high", "session_minutes": .1, "budget": {"minutes": 2, "sessions": 8, "children": 2}}
        action.update(overrides)
        with self.engine.store.tx() as tx:
            return self.engine.create_task(tx, session, action)

    def settle(self, scenario="pipeline", predicate=None):
        with patch.dict(os.environ, {"FAKE_SCENARIO": scenario}), \
             patch("labcore.resources.snapshot", return_value={"cpus": [min(os.sched_getaffinity(0))], "gpus": [], "gpu_error": None}):
            for _ in range(500):
                self.runtime.tick()
                if self.runtime.last_error:
                    self.fail(self.runtime.last_error)
                sessions = self.engine.store.all("sessions")
                requests = self.engine.store.all("requests")
                if predicate and predicate():
                    return
                active_jobs = any(j["status"] in ("running", "queued", "stopping") for j in self.engine.store.all("jobs"))
                active_tasks = any(t["status"] in ("running", "queued", "waiting_jobs", "waiting_children") for t in self.engine.store.all("tasks"))
                if not predicate and sessions and not active_jobs and not active_tasks and not any(s["status"] in ("running", "stopping") for s in sessions) and not any(r["status"] in ("queued", "running") for r in requests):
                    failures = [s for s in sessions if s["status"] == "failed"]
                    if failures:
                        self.fail(json.dumps(failures, indent=2) + "\n" + "\n".join(Path(s["stderr"]).read_text() for s in failures if s.get("stderr")))
                    return
                time.sleep(.02)
        self.fail("Runtime did not settle")

    def test_full_pipeline_includes_independent_verification(self):
        self.settle()
        tasks = self.engine.store.all("tasks")
        self.assertEqual({t["role"] for t in tasks}, {"engineer", "verifier"})
        self.assertTrue(all(t["status"] == "accepted" for t in tasks), tasks)
        self.assertTrue(all(s["status"] == "done" for s in self.engine.store.all("sessions")))
        self.bridge.deliver()
        self.assertTrue(self.client.messages)
        self.assertTrue(all(m.get("thread_ts") == "100.1" and not m.get("reply_broadcast") for m in self.client.messages))

    def test_managed_children_share_one_root_and_resume_head(self):
        self.settle("children")
        tasks = self.engine.store.all("tasks")
        head = next(t for t in tasks if t["role"] == "engineer")
        children = [t for t in tasks if t["role"] == "worker"]
        self.assertEqual(len(children), 2)
        self.assertTrue(all(t["root_task"] == head["id"] and t["status"] == "accepted" for t in children))
        self.assertEqual(head["status"], "accepted")

    def test_experiment_group_runs_as_detached_job_then_analysis(self):
        self.settle("jobs")
        jobs = self.engine.store.all("jobs")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "done", jobs)
        report = json.loads((Path(jobs[0]["run_dir"]) / "report.json").read_text())
        self.assertEqual(report["summaries"]["returns"]["mean"], 2)

    def test_no_idle_worker_or_model_calls(self):
        with self.engine.store.tx() as tx:
            for req in tx.all("requests"):
                req["status"] = "done"
                tx.put("requests", req)
        with patch.object(self.runtime.runner, "start") as start:
            for _ in range(3):
                self.runtime.tick()
            start.assert_not_called()

    def test_duplicate_slack_delivery_does_not_duplicate_work(self):
        self.engine.receive("C1", "100.1", "100.1", "human", "Repeated event")
        messages = [m for m in self.engine.store.all("messages") if m["id"] == "C1:100.1"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(len([r for r in self.engine.store.all("requests") if r["id"] == "C1:100.1"]), 1)

    def test_budget_and_profile_cannot_be_raised_by_child(self):
        parent = self.task(model="haiku", effort="low")
        with self.engine.store.tx() as tx:
            with self.assertRaises(ValueError):
                self.engine.create_task(tx, {"project": parent["project"], "channel": "C1", "thread": "100.1"},
                    {"role": "worker", "goal": "Harder", "acceptance": "Done", "model": "opus", "effort": "extra", "session_minutes": .1}, parent=parent)
        self.assertEqual(profile("Opus", "extra"), ("opus", "xhigh"))

    def test_acceptance_requires_verified_evidence(self):
        task = self.task()
        with self.engine.store.tx() as tx:
            task.update(status="awaiting_review", evidence=[{"path": "code.py"}])
            tx.put("tasks", task)
            with self.assertRaisesRegex(ValueError, "Independent"):
                self.engine._action(tx, {"id": "test", "role": "supervisor", "project": task["project"]}, None,
                                    {"type": "accept", "task": task["id"]})

    def test_invalid_action_batch_rolls_back_authorization(self):
        session = self.session()
        actions = [{"type": "assign", "role": "engineer", "goal": "Should roll back", "acceptance": "Test", "model": "sonnet", "effort": "high"},
                   {"type": "invented_operation"}]
        with self.assertRaises(ValueError):
            self.engine.apply_result(session["id"], result(actions=actions))
        self.assertFalse(self.engine.store.all("tasks"))
        self.assertFalse(self.engine.store.get("sessions", session["id"])["applied"])

    def test_two_projects_do_not_mix_conversation_or_tasks(self):
        second = self.engine.enroll("C2", "reading-book")
        self.engine.receive("C2", "101.1", "101.1", "human", "Unrelated book question")
        session = self.session()
        context = self.engine.context(session)
        self.assertNotIn("Unrelated book question", json.dumps(context["conversation"]))
        self.assertEqual(context["project"]["id"], self.channel["project"])
        self.assertNotEqual(second["project"], self.channel["project"])

    def test_quota_saves_checkpoint_and_stops_new_launches(self):
        self.settle("quota", predicate=lambda: self.engine.store.get("settings", "cooldown", {}).get("until", 0) > time.time())
        sessions = self.engine.store.all("sessions")
        self.assertEqual(sessions[0]["status"], "quota")
        self.assertTrue(Path(sessions[0]["workspace"], "checkpoint.json").exists())
        with patch.object(self.runtime.runner, "start") as start:
            restarted = Runtime(self.engine)
            restarted.dispatch()
            start.assert_not_called()
        queued = [r for r in self.engine.store.all("requests") if r["status"] == "queued"]
        self.assertTrue(queued)

    def test_reset_time_respects_provider_timestamp(self):
        until, exact = reset_time("usage limit", self.cfg, now=1000, envelope={"resets_at": 5000})
        self.assertTrue(exact)
        self.assertEqual(until, 5030)

    def test_partial_stream_preserves_completed_turn_usage(self):
        record = {"type": "assistant", "message": {"id": "message-one", "usage": {"input_tokens": 10, "output_tokens": 12}}}
        text = json.dumps(record) + "\n" + json.dumps(record) + '\n{"interrupted'
        envelope, usage = parse_output(text)
        self.assertEqual(envelope, {})
        self.assertEqual(usage['output_tokens'], 12)

    def test_closed_provider_schema_has_no_open_objects(self):
        from labcore.schema import RESULT_SCHEMA
        def visit(value):
            if isinstance(value, dict):
                if value.get('type') == 'object':
                    self.assertIs(value.get('additionalProperties'), False)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(RESULT_SCHEMA)

    def test_human_message_supersedes_pending_onboarding(self):
        other = self.engine.enroll('C3', 'new-reading-channel')
        self.assertEqual(self.engine.store.get('requests', 'onboarding:C3')['status'], 'queued')
        self.engine.receive('C3', '300.1', '300.1', 'human', 'Read this chapter with me')
        self.assertEqual(self.engine.store.get('requests', 'onboarding:C3')['status'], 'cancelled')

    def test_supervisor_can_save_notes_without_waking_a_specialist(self):
        session = self.session()
        self.engine.apply_result(session['id'], result(actions=[{'type': 'write_note', 'path': 'notes/book/chapter.md', 'text': 'A source-backed explanation'}]))
        project = self.engine.store.get('projects', session['project'])
        note = self.root / 'projects' / project['folder'] / 'notes/book/chapter.md'
        self.assertEqual(note.read_text(), 'A source-backed explanation')
        self.assertFalse(self.engine.store.all('tasks'))

    def test_supervisor_registers_incomplete_study_without_rejecting_report(self):
        session = self.session()
        self.engine.apply_result(session['id'], result(actions=[{
            'type':'update_study','study_id':'toy-study',
            'study_record':{'title':'Toy study','question':'What changes?'},'publish':True}]))
        study = self.engine.store.get('studies',session['project']+':toy-study')
        self.assertEqual(study['documentation'],'incomplete')
        self.assertTrue(study['checks']['issues'])
        self.assertEqual(self.engine.store.get('sessions',session['id'])['status'],'done')
        self.assertEqual(self.engine.context(session)['studies'][0]['study_id'],'toy-study')

    def test_ordinary_and_urgent_replies_use_correct_thread(self):
        self.engine.store.outbox("C1", "100.1", "Normal", key="normal")
        self.engine.store.outbox("C1", "100.1", "Time-sensitive alert", key="alert", urgent=True)
        self.bridge.deliver()
        self.assertFalse(self.client.messages[0]["reply_broadcast"])
        self.assertTrue(self.client.messages[1]["reply_broadcast"])
        self.assertTrue(all(m["thread_ts"] == "100.1" for m in self.client.messages))

    def test_archive_and_pause_do_not_delete_records(self):
        task = self.task()
        self.bridge.command(self.channel, "100.1", "close this thread", "102.1")
        self.assertEqual(self.engine.store.get("tasks", task["id"])["status"], "cancelled")
        self.assertTrue(self.engine.store.all("messages"))
        self.bridge.command(self.channel, "100.1", "pause this project", "103.1")
        self.assertEqual(self.engine.store.get("projects", task["project"])["status"], "paused")

    def test_read_only_peer_workspace_is_enforced_for_normal_writes(self):
        task = self.task()
        session = self.session("engineer", task)
        own = Path(session["workspace"])
        peer = own.parent / "peer"
        peer.mkdir()
        target = peer / "result.txt"
        target.write_text("Do not change")
        env = self.runtime.runner.environment(own)
        script = "from pathlib import Path; Path(" + repr(str(target)) + ").write_text('broken')"
        command = self.runtime.runner.sandbox_command([sys.executable, "-c", script], session, env)
        completed = subprocess.run(command, cwd=own, env=env, capture_output=True, text=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(target.read_text(), "Do not change")
        self.assertIn("Permission", completed.stderr)

    def test_sandbox_cli_status_checkpoint_and_private_files(self):
        task = self.task()
        session = self.session("engineer", task)
        own = Path(session["workspace"])
        env = self.runtime.runner.environment(own)
        env.update(LAB_SESSION=session["id"])
        self.runtime.status_views()
        def run(args):
            command = self.runtime.runner.sandbox_command(
                [sys.executable, str(self.root / "lab.py")] + args, session, env)
            return subprocess.run(command, cwd=own, env=env, capture_output=True, text=True)
        status = run(["work", "status"])
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("budget_usage", json.loads(status.stdout))
        checkpoint = run(["work", "checkpoint", "--completed", "Checked sandbox", "--next-step", "Review"])
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)
        self.assertEqual(json.loads((own / "checkpoint.json").read_text())["next_step"], "Review")
        private = self.root / ".slack.env"
        private.write_text("SECRET=private")
        script = "from pathlib import Path; print(Path(" + repr(str(private)) + ").read_text())"
        cmd = self.runtime.runner.sandbox_command([sys.executable, "-c", script], session, env)
        denied = subprocess.run(cmd, cwd=own, env=env, capture_output=True, text=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("Permission", denied.stderr)

    def test_launch_diagnostics_explain_daily_budget_and_clear_after_raise(self):
        task = self.task(session_minutes=1, budget={"minutes": 2, "sessions": 8, "children": 2})
        with self.engine.store.tx() as tx:
            project = tx.get("projects", task["project"])
            project["daily_minutes"] = 0.5
            tx.put("projects", project)
        self.runtime.status_views()
        saved = self.engine.store.get("tasks", task["id"])
        self.assertEqual(saved["launch_hold"]["reason"], "project daily session-time budget")
        self.assertEqual(saved["launch_hold"]["requested_minutes"], 1)
        self.assertGreater(saved["launch_hold"]["retry_at"], time.time())
        supervisor = self.session()
        context = self.engine.context(supervisor)
        self.assertEqual(context["tasks"][0]["launch_hold"]["reason"], "project daily session-time budget")
        self.assertEqual(context["budget_usage"]["project_limit_minutes"], 0.5)
        with self.engine.store.tx() as tx:
            project = tx.get("projects", task["project"])
            project["daily_minutes"] = 360
            tx.put("projects", project)
        self.runtime.status_views()
        self.assertIsNone(self.engine.store.get("tasks", task["id"])["launch_hold"])
        self.assertEqual(self.engine.store.get("scheduler", task["project"])["budget_usage"]["project_limit_minutes"], 360)
        self.runtime.runner.start = lambda session: None
        self.runtime.dispatch()
        self.assertEqual(self.engine.store.get("tasks", task["id"])["status"], "running")

    def test_global_daily_hold_is_distinct_and_closed_threads_never_launch(self):
        task = self.task()
        with self.engine.store.tx() as tx:
            self.cfg["budgets"]["daily_agent_minutes"] = 0
            hold = self.engine.task_launch_hold(tx, task)
            self.assertEqual(hold["reason"], "global daily session-time budget")
            self.cfg["budgets"]["daily_agent_minutes"] = 360
            tx.put("closed_threads", {"id": "C1:100.1"})
            self.assertEqual(self.engine.task_launch_hold(tx, task)["reason"], "discussion closed")
        self.runtime.runner.start = lambda session: self.fail("closed thread must not launch")
        self.runtime.dispatch()
        self.assertEqual(self.engine.store.get("tasks", task["id"])["status"], "queued")

    def test_report_publication_rejects_escape_and_auth_files(self):
        task = self.task()
        session = self.session("engineer", task)
        for path in ("../../escape", ".auth/.credentials.json"):
            with self.assertRaises(ValueError):
                self.engine.apply_result(session["id"], result(evidence=[{"path": path, "description": "invalid"}]))

    def test_preemption_requests_checkpoint_without_losing_task(self):
        low = self.task(key="low", priority=10)
        high = self.task(key="high", priority=90)
        session = self.session("engineer", low)
        session["priority"] = 10
        with self.engine.store.tx() as tx:
            tx.put("sessions", session)
        self.runtime.preempt()
        self.assertTrue(self.engine.store.get("sessions", session["id"])["preempted"])
        self.assertEqual(self.engine.store.get("tasks", high["id"])["status"], "queued")

    def test_head_queue_runs_only_one_engineer_and_prefers_priority(self):
        low = self.task(key="low", priority=10)
        high = self.task(key="high", priority=95)
        with self.engine.store.tx() as tx:
            for req in tx.all("requests"):
                req["status"] = "done"
                tx.put("requests", req)
        with patch.object(self.runtime.runner, "start"):
            self.runtime.dispatch()
            self.runtime.dispatch()
        heads = [s for s in self.engine.store.all("sessions") if s["role"] == "engineer"]
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0]["task"], high["id"])
        self.assertEqual(self.engine.store.get("tasks", low["id"])["status"], "queued")

    def test_child_count_and_aggregate_session_budget_are_enforced(self):
        parent = self.task(budget={"minutes": 1, "sessions": 1, "children": 0})
        session = self.session("engineer", parent)
        with self.engine.store.tx() as tx:
            with self.assertRaisesRegex(ValueError, "worker allowance"):
                self.engine.create_task(tx, session, {"role": "worker", "goal": "Child", "acceptance": "Done",
                    "model": "haiku", "effort": "low", "session_minutes": .1}, parent=parent)
            reason = self.engine.budget_available(tx, tx.get("projects", parent["project"]), .1, parent["id"])
            self.assertEqual(reason, "assignment session limit")

    def test_timeout_grace_and_durable_reason(self):
        task = self.task()
        session = self.session("engineer", task)
        job = self.runtime.jobs.submit(session, {"command": ["sleep", "100"], "hours": .01}, "timeout")
        run_dir = Path(job["run_dir"])
        run_dir.mkdir(parents=True)
        job.update(status="running", pid=123456, identity="test-process", deadline=time.time() - 1)
        with self.engine.store.tx() as tx:
            tx.put("jobs", job)
        with patch("labcore.jobs.identity", return_value="test-process"), patch("labcore.jobs.os.killpg") as kill:
            self.runtime.jobs.poll()
            stopped = self.engine.store.get("jobs", job["id"])
            self.assertEqual(stopped["status"], "stopping")
            self.assertEqual(stopped["stop_kind"], "time_limit")
            self.assertGreater(stopped["stop_at"], time.time() + 55)
            kill.assert_called_once_with(123456, 15)
            self.runtime.jobs.poll()
            self.assertEqual(kill.call_count, 1)
            stopped["stop_at"] = time.time() - 1
            with self.engine.store.tx() as tx:
                tx.put("jobs", stopped)
            self.runtime.jobs.poll()
            self.assertEqual(kill.call_args.args, (123456, 9))
        # Recovery after a restart with no wrapper exit receipt still records cause.
        with patch("labcore.jobs.identity", return_value=None):
            self.runtime.jobs.poll()
        final = self.engine.store.get("jobs", job["id"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["exit_code"], 124)
        self.assertEqual(json.loads((run_dir / "exit.json").read_text())["reason"], "time limit")
        self.assertEqual(json.loads((run_dir / "report.json").read_text())["stop_kind"], "time_limit")

    def test_expensive_job_requires_human_approval(self):
        self.cfg['budgets']['expensive_job_hours'] = 2
        task = self.task(resources={"total_job_hours": 8})
        session = self.session("engineer", task)
        job = self.runtime.jobs.submit(session, {"command": ["echo", "test"], "hours": 3}, "request-one")
        self.assertEqual(job["status"], "approval")
        with patch.object(self.runtime.jobs, "_launch") as launch:
            self.runtime.jobs.start_ready()
            launch.assert_not_called()
        self.bridge.approval({"user": {"id": "human"}, "actions": [{"action_id": "approve_job", "value": job["id"]}]})
        self.assertEqual(self.engine.store.get("jobs", job["id"])["status"], "queued")

    def test_job_requests_are_idempotent_and_cannot_share_metrics_path(self):
        task = self.task()
        session = self.session("engineer", task)
        request = {"command": ["echo", "test"], "hours": .01, "metrics": "metrics.json"}
        first = self.runtime.jobs.submit(session, request, "same-request")
        again = self.runtime.jobs.submit(session, request, "same-request")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.engine.store.all("jobs")), 1)
        with self.assertRaisesRegex(ValueError, "separate metrics"):
            self.runtime.jobs.submit(session, request, "different-request")

    def test_specialist_cannot_grant_itself_a_larger_budget(self):
        task = self.task()
        session = self.session("engineer", task)
        with self.engine.store.tx() as tx:
            with self.assertRaisesRegex(ValueError, "not authorized"):
                self.engine._action(tx, session, task, {"type": "grant", "task": task["id"], "budget": {"minutes": 100}})

    def test_quota_recovery_resumes_saved_request_after_restart(self):
        self.settle("quota", predicate=lambda: self.engine.store.get("settings", "cooldown", {}).get("until", 0) > time.time())
        old = self.engine.store.all("sessions")[0]
        with self.engine.store.tx() as tx:
            tx.put("settings", {"id": "cooldown", "until": 0})
        self.runtime = Runtime(self.engine)
        self.settle()
        resumed = [s for s in self.engine.store.all("sessions") if old["id"] in s.get("resume_from", [])]
        self.assertTrue(resumed)
        self.assertEqual(self.engine.store.get("requests", "C1:100.1")["status"], "done")
        self.assertIn("Read existing evidence", Path(old["workspace"], "checkpoint.json").read_text())

    def test_result_application_is_idempotent(self):
        session = self.session()
        value = result(reply="Evidence saved")
        self.engine.apply_result(session["id"], value)
        self.engine.apply_result(session["id"], value)
        self.assertEqual(len(self.engine.store.all("outbox")), 1)

    def test_bounded_meeting_records_turns_and_resumes_requester(self):
        origin = self.task()
        supervisor = self.session()
        self.engine.apply_result(supervisor["id"], result(actions=[{"type": "meeting", "task": origin["id"],
            "question": "Which normalization matches the paper?", "model": "sonnet", "effort": "medium", "session_minutes": .1, "turns": 2}]))
        for _ in range(2):
            candidate = next(t for t in self.engine.store.all("tasks") if t.get("meeting") and t["status"] == "queued")
            session = self.session(candidate["role"], candidate)
            Path(session["workspace"], "answer.md").write_text("A source-backed clarification")
            self.engine.apply_result(session["id"], result(evidence=[{"path": "answer.md", "description": "Clarification"}]))
        meeting = self.engine.store.all("meetings")[0]
        self.assertEqual(meeting["status"], "done")
        self.assertEqual(len(meeting["reports"]), 2)
        self.assertEqual(self.engine.store.get("tasks", origin["id"])["status"], "queued")

    def test_paper_records_are_versioned_and_bad_batches_do_not_publish(self):
        task = self.task(role="literature", goal="Read paper")
        note = {"canonical_id": "arxiv:2405.19153", "title": "A paper", "authors": ["Author"], "year": 2024,
                "url": "https://arxiv.org/abs/2405.19153", "read_depth": "full", "sections_read": ["Method", "Experiments"],
                "method": "A checked method", "results": ["A reported result"], "limitations": ["One benchmark"],
                "supporting_sections": ["Section 4"]}
        first = self.session("literature", task)
        atomic_json(Path(first["workspace"]) / "paper.json", note)
        with self.assertRaises(ValueError):
            self.engine.apply_result(first["id"], result(actions=[{"type": "publish_paper", "path": "paper.json"}, {"type": "invalid"}]))
        self.assertFalse(self.engine.store.all("papers"))
        self.assertFalse(list((self.root / "library/papers").glob("*/note.json")))
        self.engine.apply_result(first["id"], result(actions=[{"type": "publish_paper", "path": "paper.json"}]))
        second = self.session("literature", task)
        atomic_json(Path(second["workspace"]) / "paper.json", dict(note, limitations=["Newly checked limitation"]))
        self.engine.apply_result(second["id"], result(actions=[{"type": "publish_paper", "path": "paper.json"}]))
        self.assertEqual(self.engine.store.all("papers")[0]["version"], 2)
        self.assertEqual(len(list((self.root / "library/papers").glob("*/v*.json"))), 2)

    def test_promoted_code_uses_frozen_reviewed_source(self):
        task = self.task(requires_verification=False)
        author = self.session("engineer", task)
        source = Path(author["workspace"]) / "src" / "baseline.py"
        source.parent.mkdir()
        source.write_text("VERSION = 1\n")
        self.engine.apply_result(author["id"], result(evidence=[{"path": "src/baseline.py", "description": "Checked source"}]))
        source.write_text("VERSION = 2\n")
        supervisor = self.session()
        self.engine.apply_result(supervisor["id"], result(actions=[{"type": "accept", "task": task["id"]}, {"type": "promote_code", "task": task["id"]}]))
        project = self.engine.store.get("projects", task["project"])
        published = self.root / "projects" / project["folder"] / "code/revisions" / task["id"] / "src/baseline.py"
        self.assertEqual(published.read_text(), "VERSION = 1\n")

    def test_link_channel_shares_project_without_merging_unrelated_tasks(self):
        second = self.engine.enroll("C2", "related-reading")
        session = self.session()
        session.update(project=second["project"], channel="C2")
        with self.engine.store.tx() as tx:
            tx.put("sessions", session)
        self.engine.apply_result(session["id"], result(actions=[{"type": "link_channel", "project": self.channel["project"]}]))
        self.assertEqual(self.engine.store.get("channels", "C2")["project"], self.channel["project"])
        self.assertEqual(self.engine.store.get("projects", second["project"])["status"], "paused")

    def test_live_session_is_adopted_after_controller_restart(self):
        with patch.dict(os.environ, {"FAKE_SCENARIO": "pipeline", "FAKE_DELAY": ".1"}):
            self.runtime.dispatch()
            first = self.engine.store.all("sessions")[0]
            for _ in range(100):
                if self.engine.store.get("sessions", first["id"]).get("pid"):
                    break
                time.sleep(.01)
            restarted = Runtime(self.engine)
            for _ in range(100):
                restarted.runner.poll()
                if self.engine.store.get("sessions", first["id"])["status"] == "done":
                    break
                time.sleep(.01)
        self.assertEqual(self.engine.store.get("sessions", first["id"])["status"], "done")
        self.assertEqual(len(self.engine.store.all("sessions")), 1)

    def test_delayed_cancelled_launcher_does_not_start_a_model(self):
        session = self.session()
        with self.engine.store.tx() as tx:
            session["status"] = "failed"
            tx.put("sessions", session)
        marker = self.root / "should-not-exist"
        command = [sys.executable, str(self.root / "labcore/sessionexec.py"), "--db", str(self.engine.store.path),
                   "--session", session["id"], "--", sys.executable, "-c", "open(" + repr(str(marker)) + ",'w').write('bad')"]
        done = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(marker.exists())

    def test_gpu_probe_excludes_busy_and_reserved_devices(self):
        from labcore.resources import snapshot
        gpu = type("Output", (), {"stdout": "0, GPU-a, 1000, 999, 0\n1, GPU-b, 1000, 999, 0\n2, GPU-c, 1000, 999, 0\n"})()
        processes = type("Output", (), {"stdout": "GPU-b\n"})()
        with patch("labcore.resources.subprocess.run", side_effect=[gpu, processes]), \
             patch("labcore.resources.os.sched_getaffinity", return_value=set(range(8))), \
             patch("labcore.resources.os.getloadavg", return_value=(2, 2, 2)):
            free = snapshot(dict(self.cfg["resources"], reserve_cpu_threads=1),
                            [{"status": "running", "allocation": {"cpus": [0], "gpus": ["GPU-c"]}}])
        self.assertEqual(free["gpus"], ["GPU-a"])
        self.assertEqual(len(free["cpus"]), 5)

    def test_large_context_preview_remains_valid_json_and_links_full_record(self):
        for index in range(12):
            self.engine.receive('C1', 'long-' + str(index), '100.1', 'human', 'Detailed request ' + 'x' * 5000)
        session = self.session()
        (Path(session['workspace']) / 'control').mkdir()
        system, user = self.runtime.runner.prompt(session)
        data = json.loads(user)
        self.assertEqual(data['full_context_path'], 'control/context.json')
        self.assertLess(len(user), 35000)
        full = json.loads((Path(session['workspace']) / 'control/context.json').read_text())
        self.assertTrue(any(len(m['text']) > 5000 for m in full['conversation']))

    def test_supervisor_can_prepare_prompt_with_mixed_slack_timestamps_after_reset(self):
        now=time.time()
        with self.engine.store.tx() as tx:
            project=tx.get('projects',self.channel['project']);project['writing_reset_at']=now-10;tx.put('projects',project)
            for i,(created,stamp,text) in enumerate([(now-100,now-100,'OLD_FORMAT_RULES'),
                    (now,now,'numeric bot timestamp'),('2099-01-01T00:00:00+00:00',now,'ISO human timestamp'),
                    (None,str(now),'Slack string timestamp'),('unknown','unknown','preserve unknown timestamp')]):
                tx.put('messages',dict(id='mixed-'+str(i),project=project['id'],channel='C1',thread='100.1',
                    user='supervisor',created=created,time=stamp,text=text))
        session=self.session();(Path(session['workspace'])/'control').mkdir(exist_ok=True)
        _,user=self.runtime.runner.prompt(session);preview=json.loads(user)
        messages=[m['text'] for m in preview['conversation']]
        self.assertNotIn('OLD_FORMAT_RULES',messages)
        for text in ('numeric bot timestamp','ISO human timestamp','Slack string timestamp','preserve unknown timestamp'):
            self.assertIn(text,messages)

    def test_study_source_reports_survive_recent_task_preview_limit(self):
        source=self.task(key='original-study-source')
        with self.engine.store.tx() as tx:
            source.update(status='accepted',report='projects/study/reports/original/report.json');tx.put('tasks',source)
            tx.put('studies',dict(id=source['project']+':study',project=source['project'],study_id='study',task_ids=[source['id']]))
            for i in range(35):
                tx.put('tasks',dict(source,id='unrelated-'+str(i),key='unrelated-'+str(i),report=None))
        current=self.task(key='write-study',study_id='study',role='literature')
        context=self.engine.context(self.session('literature',current))
        peer=next(t for t in context['tasks'] if t['id']==source['id'])
        self.assertEqual(peer['report'],source['report'])

    def test_table_layout_cannot_be_replaced_by_a_supervisor_note_action(self):
        self.cfg['research_story']={'format':'study_table'}
        session=self.session()
        with self.engine.store.tx() as tx:
            with self.assertRaisesRegex(ValueError,'generated study table'):
                self.engine._action(tx,session,None,dict(type='write_note',path='notes/research-story.md',text='# Rewrite all studies'))

    def test_resuming_a_session_clears_old_preemption_flag(self):
        task = self.task()
        session = self.session('engineer', task)
        control = Path(session['workspace']) / 'control'
        control.mkdir()
        atomic_json(control / 'PREEMPT.json', {'checkpoint_now': True})
        self.runtime.runner.start(session)
        self.assertFalse((control / 'PREEMPT.json').exists())

    def test_inactive_channel_does_not_launch_queued_models(self):
        self.bridge.archived({'channel': 'C1'})
        with patch.object(self.runtime.runner, 'start') as start:
            self.runtime.dispatch()
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
