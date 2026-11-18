"""Nonblocking Claude sessions, durable checkpoints, and usage-limit backoff."""
from __future__ import annotations

import datetime as dt
import copy
import ctypes
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .files import atomic_json
from .schema import RESULT_SCHEMA
from . import delegation, auth, accounts, history, customization


def identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if stat[0] == "Z" else stat[19]
    except (OSError, IndexError, TypeError):
        return None


def message_after_reset(message, cutoff):
    """Human and bot records use ISO dates and numeric timestamps respectively."""
    for value in (message.get('created'),message.get('time')):
        try:
            stamp=float(value)
        except (ValueError,TypeError,OverflowError):
            try:stamp=dt.datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
            except (ValueError,TypeError,OverflowError):continue
        if math.isfinite(stamp):return stamp>=cutoff
    return True  # An unknown timestamp must not silently drop a human constraint.


def parse_output(text, *, resumed=False, model_usage_baseline=None):
    """Accept the native JSONL stream and the single-JSON offline adapter."""
    records = []
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            records = [value]
    except ValueError:
        for line in text.splitlines():
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    records.append(item)
            except ValueError:
                continue  # An interrupted last line does not invalidate earlier usage records.
    results = [r for r in records if r.get('type') == 'result' or 'structured_output' in r or 'is_error' in r]
    meaningful = [r for r in results if r.get('is_error') or isinstance(r.get('structured_output'), dict)
                  or str(r.get('result') or '').strip()]
    final = dict(meaningful[-1] if meaningful else results[-1] if results else {})
    # Native helpers may append empty zero-turn successes after the controller's
    # report or quota error. They must not erase either. Keep the latest cumulative
    # accounting separately: helpers can finish after the meaningful response.
    aggregate = next((r['modelUsage'] for r in reversed(results) if r.get('modelUsage')), None)
    if aggregate is not None:
        final['modelUsage'] = aggregate
    if final.get('is_error'):
        prior = next((r['structured_output'] for r in reversed(results)
                      if isinstance(r.get('structured_output'), dict) and not r.get('is_error')), None)
        if prior is not None:
            final['saved_partial_output'] = prior
    usage = {}
    messages = {}
    for record in records:
        message = record.get("message", {}) if record.get("type") == "assistant" else {}
        if not message.get('id'):
            continue
        previous = messages.setdefault(message['id'], {})
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            value = message.get("usage", {}).get(key)
            if isinstance(value, (int, float)):
                previous[key] = max(previous.get(key, 0), value)
    for values in messages.values():
        for key, value in values.items():
            usage[key] = usage.get(key, 0) + value
    # modelUsage includes native helpers AND the inherited totals of resumed
    # histories. Subtract the parent's raw model totals before charging this run.
    # If the baseline is unavailable, use current final/stream usage only.
    model_usage = {}
    for name, model in (final.get('modelUsage') or {}).items():
        if resumed and model_usage_baseline is None:
            continue
        baseline = (model_usage_baseline or {}).get(name, {})
        for camel, key in (('inputTokens', 'input_tokens'), ('outputTokens', 'output_tokens'),
                           ('cacheReadInputTokens', 'cache_read_input_tokens'),
                           ('cacheCreationInputTokens', 'cache_creation_input_tokens')):
            if isinstance(model.get(camel), (int, float)):
                previous = baseline.get(camel, 0)
                model_usage[key] = model_usage.get(key, 0) + max(0, model[camel] - (previous if isinstance(previous, (int, float)) else 0))
    reported = {}
    for result in results:
        for key, value in (result.get('usage') or {}).items():
            if isinstance(value, (int, float)):
                reported[key] = max(reported.get(key, 0), value)
    return final, {k: max(usage.get(k, 0), reported.get(k, 0) or 0, model_usage.get(k, 0))
                   for k in set(usage) | set(reported) | set(model_usage)}


def parse_session_output(store, session, text):
    parent_id = session.get('history_parent')
    baseline = None
    if parent_id:
        parent = store.get('sessions', parent_id, {})
        try:
            previous, _ = parse_output(Path(parent['stdout']).read_text(errors='replace'))
            baseline = previous.get('modelUsage')
        except (OSError, KeyError):
            pass
    return parse_output(text, resumed=bool(parent_id), model_usage_baseline=baseline)


def reset_time(text, cfg, now=None, envelope=None):
    now = now or time.time()
    # Prefer machine-readable reset times when the provider supplies them.
    def find(value):
        if isinstance(value, dict):
            for key, v in value.items():
                if key.lower().replace("_", "") in ("resetsat", "resetat", "ratelimitresetat"):
                    try:
                        epoch = float(v)
                        if epoch > 1e12:
                            epoch /= 1000
                        if epoch > now:
                            return epoch + 30
                    except (ValueError, TypeError):
                        pass
                found = find(v)
                if found:
                    return found
        elif isinstance(value, list):
            for v in value:
                found = find(v)
                if found:
                    return found
        return None
    supplied = find(envelope or {})
    if supplied:
        return supplied, True
    match = re.search(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(?:\(([^)]+)\))?", text, re.I)
    if match:
        try:
            zone = ZoneInfo(match[4] or cfg.get("timezone", "UTC"))
            current = dt.datetime.fromtimestamp(now, zone)
            hour = int(match[1]) % 12 + (12 if match[3].lower() == "pm" else 0)
            reset = current.replace(hour=hour, minute=int(match[2] or 0), second=30, microsecond=0)
            if reset.timestamp() <= now:
                reset += dt.timedelta(days=1)
            return reset.timestamp(), True
        except (ValueError, KeyError):
            pass
    return now + cfg["scheduling"].get("retry_minutes", 120) * 60, False


class Runner:
    def __init__(self, engine):
        self.engine = engine
        self.cfg = engine.cfg
        self.processes = {}
        self._conda = None

    def environment(self, workspace, account=None):
        env = dict(os.environ)
        if not self.cfg["claude"].get("use_api_key", False):
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if account:
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            env.pop("CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", None)
        if self.cfg["claude"].get("shared_memory", True):
            env.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
        env.pop("CLAUDE_CODE_RETRY_WATCHDOG", None)
        # No inherited machine-wide setting may silently upgrade delegation.
        for key in ('CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE',
                    'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS', 'CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH',
                    'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS', 'CLAUDE_CODE_DISABLE_ALL_HOOKS',
                    'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'):
            env.pop(key, None)
        env.update(CLAUDE_CODE_MAX_RETRIES=str(self.cfg["claude"].get("api_retries", 1)),
                   API_TIMEOUT_MS=str(self.cfg["claude"].get("api_timeout_ms", 60000)),
                   CLAUDE_CODE_MAX_TURNS=str(self.cfg["claude"].get("max_turns", 40)))
        self.python_environment(env)
        work = Path(workspace)
        for sub in ("tmp", ".cache", "control"):
            (work / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        # Share the CLI's own credential refresh lock/cache across sessions. This
        # private runtime directory is never published as a project artifact.
        if account:
            auth = accounts.directory(self.cfg, account)
            auth.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            auth = self.engine.root / "runtime" / "claude-auth" / "config"
            auth.mkdir(parents=True, exist_ok=True, mode=0o700)
            credential = Path.home() / ".claude" / ".credentials.json"
            link = auth / ".credentials.json"
            sync = auth / "credential-source.json"
            previous = json.loads(sync.read_text()) if sync.exists() else {}
            if credential.exists() and (not link.exists() or previous.get("mtime_ns") != credential.stat().st_mtime_ns):
                shutil.copyfile(credential, link)
                link.chmod(0o600)
                atomic_json(sync, {"mtime_ns": credential.stat().st_mtime_ns})
            old_config = Path.home() / ".claude.json"
            config_link = auth / ".claude.json"
            if old_config.exists() and not config_link.exists():
                # This config is session-local because Claude may update it.
                data = json.loads(old_config.read_text())
                keep = {k: v for k, v in data.items() if k in ("oauthAccount", "hasCompletedOnboarding", "lastOnboardingVersion")}
                atomic_json(config_link, keep)
                # Claude versions also keep their main config next to the config
                # directory (CLAUDE_CONFIG_DIR + '.json'), not inside it.
                if not auth.with_suffix(".json").exists():
                    atomic_json(auth.with_suffix(".json"), keep)
        self.compute_environment(env, work)
        env.update(CLAUDE_CONFIG_DIR=str(auth), TMPDIR=str(work / "tmp"), XDG_CACHE_HOME=str(work / ".cache"),
                   CUDA_VISIBLE_DEVICES="", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
                   LAB_ROOT=str(self.engine.root), LAB_WORKSPACE=str(work))
        return env

    def python_environment(self, env):
        name = self.cfg["claude"].get("python_env")
        if name and self._conda is None:
            conda = shutil.which("conda") or os.environ.get("CONDA_EXE")
            if not conda:
                raise ValueError("conda is unavailable; activate claude-env or set claude.python_env to null")
            result = subprocess.run([conda, "env", "list", "--json"], capture_output=True, text=True, check=True, timeout=15)
            self._conda = next((p for p in json.loads(result.stdout)["envs"] if Path(p).name == name), None)
            if not self._conda:
                raise ValueError(f"Conda environment {name} is missing")
        if self._conda:
            env.update(PATH=str(Path(self._conda) / "bin") + ":" + env.get("PATH", ""), CONDA_PREFIX=self._conda,
                       CONDA_DEFAULT_ENV=name, PYTHONNOUSERSITE="1")

    def compute_environment(self, env, work):
        cache = work / ".cache"
        for variable, subdirectory in {
                "TRITON_CACHE_DIR": "triton", "TORCHINDUCTOR_CACHE_DIR": "inductor",
                "CUDA_CACHE_PATH": "cuda", "MPLCONFIGDIR": "matplotlib"}.items():
            path = cache / subdirectory
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            env[variable] = str(path)
        if self.cfg["claude"].get("sandbox", True):
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.syscall(444, 0, 0, 1) == 1:
                env.setdefault("TRITON_CACHE_MANAGER", "triton_cache:SameDirectoryCacheManager")
                env["PYTHONPATH"] = str(self.engine.root / "labcore") + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        # Triton compiles driver helpers even without torch.compile. Use an
        # installed CUDA SDK's headers without modifying the shared environment.
        for toolkit in (Path(env.get("CUDA_HOME", "/nonexistent")), Path("/usr/local/cuda"), Path("/usr/local/cuda-12.5"), Path("/usr/local/cuda-11.8")):
            include = toolkit / "include"
            if not (include / "cuda.h").is_file():
                include = toolkit / "targets/x86_64-linux/include"
            if (include / "cuda.h").is_file():
                env["CPATH"] = str(include) + (":" + env["CPATH"] if env.get("CPATH") else "")
                env["CUDA_HOME"] = str(toolkit)
                break

    def prompt(self, session):
        common = (self.engine.root / "prompts" / "common.md").read_text()
        role = (self.engine.root / "prompts" / ("supervisor.md" if session["role"] == "supervisor" else "specialist.md")).read_text()
        context = self.engine.context(session)
        path = Path(session["workspace"]) / "control" / "context.json"
        atomic_json(path, context)
        # Context stays bounded, but full local sources and checkpoints are linked.
        compact = copy.deepcopy(context)
        compact["full_context_path"] = "control/context.json"
        if session['role']=='supervisor' and context['project'].get('writing_reset_at'):
            cutoff=context['project']['writing_reset_at']
            compact['conversation']=[m for m in compact['conversation'] if message_after_reset(m,cutoff)]
            compact['report_reset_notice']='Report workflow reset. Earlier discussion remains in full_context_path; use the current report-workflow brief rather than old bulk-writing instructions.'
        from .documentation_policy import is_documentation, compact_context
        if self.cfg.get('documentation',{}).get('enabled',False) and is_documentation(context.get('task') or {}):
            compact=compact_context(compact)
            role=(self.engine.root/'prompts/documentation.md').read_text()
            common=common.replace('Minimizing usage is not the goal.',
                'Reuse accepted work and minimize repeated reconstruction; documentation policy ceilings apply.')
        for message in compact["conversation"]:
            if len(message["text"]) > 2400:
                message["text"] = message["text"][:2300] + "\n[Excerpt: read full_context_path before acting on this message.]"
        compact["project"]["brief"] = compact["project"].get("brief", "")[:6000]
        for task in compact.get("tasks", []):
            task["summary"] = str(task.get("summary") or "")[:1000]
            task["notes"] = task.get("notes", [])[-4:]
        for key in ("checkpoint", "root_allocation"):
            if len(json.dumps(compact.get(key))) > 5000:
                compact[key] = {"excerpt": json.dumps(compact[key])[:4000], "full_record": "control/context.json"}
        compact["resume_from"] = [{"workspace": old["workspace"], "error": str(old.get("error") or "")[:500],
                                    "checkpoint_excerpt": json.dumps(old["checkpoint"])[:1500]} for old in compact.get("resume_from", [])[-3:]]
        while len(json.dumps(compact)) > 26000 and len(compact["conversation"]) > 2:
            compact["conversation"].pop(0)
        while len(json.dumps(compact)) > 26000 and len(compact["tasks"]) > 4:
            compact["tasks"].pop(0)
        if len(json.dumps(compact)) > 28000:
            compact = {"role": session["role"], "project": {k: project for k, project in context["project"].items() if k != "brief"},
                       "task_id": session.get("task"), "resource_policy": context["resource_policy"], "experiment_limits": context["experiment_limits"], "assignment_limits": context["assignment_limits"], "requests": session.get("reasons", []), "full_context_path": "control/context.json",
                       "notice": "Large context. Read the full work order, checkpoints, and latest human request before acting."}
        user = json.dumps(compact, indent=2)
        if session['role']=='supervisor' or (self.cfg.get('documentation',{}).get('enabled',False) and is_documentation(context.get('task') or {})):
            role+='\n\n'+(self.engine.root/'prompts/report-workflow.md').read_text()
        return common + "\n" + role, user

    def sandbox_reads(self, cmd, session):
        root = self.engine.root
        project = self.engine.store.get("projects", session["project"])
        executable = Path(shutil.which(cmd[0]) or cmd[0]).resolve()
        reads = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/dev", "/proc", "/sys", "/opt",
                 "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/run/systemd/resolve",
                 executable.parent, Path(sys.executable).resolve().parents[2], root / "labcore", root / "lab.py",
                 root / "prompts", root / "library", root / "projects" / project["folder"],
                 root / "workspaces" / project["id"]]
        if not session.get("account"):
            reads.append(Path.home() / ".claude" / ".credentials.json")
        history_directory = history.project_directory(self.engine, session["project"])
        if history_directory.exists():
            reads.append(history_directory)
        if self._conda:
            reads.append(self._conda)
        for project_id in project.get("linked_projects", []):
            linked = self.engine.store.get("projects", project_id)
            if linked:
                reads.append(root / "projects" / linked["folder"])
        if len(cmd) > 1 and cmd[1].endswith(".py") and Path(cmd[1]).is_file():
            reads.append(cmd[1])  # explicit Python CLI adapter (including offline test doubles)
        return reads

    def sandbox_command(self, cmd, session, env):
        if not self.cfg["claude"].get("sandbox", True):
            return cmd
        root = self.engine.root
        reads = self.sandbox_reads(cmd, session)
        wrapper = [sys.executable, str(root / "labcore" / "sandbox.py")]
        for path in reads:
            if Path(path).exists():
                wrapper += ["--read", str(path)]
        if self.cfg["claude"].get("shared_memory", True):
            memory = customization.memory_directory(self.engine, session["project"])
            memory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for package in session.get("shared_plugin_paths", []):
            wrapper += ["--read", package]
        writes = [session["workspace"], "/dev/null", "/dev/shm"]
        writes += [Path(env["CLAUDE_CONFIG_DIR"]), Path(env["CLAUDE_CONFIG_DIR"]).with_name(Path(env["CLAUDE_CONFIG_DIR"]).name + ".json")]
        if self.cfg["claude"].get("shared_memory", True):
            writes.append(customization.memory_directory(self.engine, session["project"]))
        writes += [str(p) for p in Path("/dev").glob("nvidia*")]
        if Path("/dev/dri").exists():
            writes.append("/dev/dri")
        for path in writes:
            if Path(path).exists():
                wrapper += ["--write", str(path)]
        if session.get("gpu_job"):
            wrapper += ["--gpu-process"]
        return wrapper + ["--"] + cmd

    def start(self, session):
        workspace = Path(session["workspace"])
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = self.environment(workspace, session.get("account"))
        (workspace / "control" / "PREEMPT.json").unlink(missing_ok=True)
        env.update(LAB_SESSION=session["id"], LAB_ROLE=session["role"])
        from .documentation_policy import is_documentation
        task = self.engine.store.get('tasks', session.get('task', ''), {})
        doc_work = self.cfg.get('documentation', {}).get('enabled', False) and is_documentation(task)
        if doc_work:
            session['work_kind'] = 'documentation'
        turn_limit = self.cfg['claude'].get('max_turns', 150)
        history_flags = history.prepare(self.engine, session)
        native_flags = delegation.prepare(self.engine, session, env)
        native_enabled = bool(native_flags)
        tools = delegation.TOOLS + (',' + delegation.NATIVE_TOOLS if native_flags else '')
        native_flags = customization.prepare(self.engine, session, native_flags)
        if session.get("shared_plugin_paths"):
            tools += ",Skill"
        system, user = self.prompt(session)
        atomic_json(workspace / "control" / "status.json", self.engine.context(session))
        logs = self.engine.root / "runtime" / "sessions" / session["id"]
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        (logs / "prompt.txt").write_text(system + "\n\n" + user)
        binary = self.cfg["claude"]["bin"]
        binary = str(self.engine.root / binary) if "/" in binary and not Path(binary).is_absolute() else binary
        cmd = ([sys.executable, binary] if binary.endswith(".py") else [binary]) + [
            "-p", user, "--append-system-prompt", system, "--output-format", "stream-json", "--verbose", "--json-schema", json.dumps(RESULT_SCHEMA),
            "--exclude-dynamic-system-prompt-sections",
            "--model", session["model"], "--effort", session["effort"], "--session-id", session["id"],
            "--max-turns", str(turn_limit),
            "--tools", tools,
            "--allowedTools", tools,
            "--disallowedTools", "TeamCreate,TeamDelete" if native_enabled else "Agent,Task,SendMessage", "--permission-mode", "acceptEdits", "--permission-prompts", "none",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
            ]
        if not session.get("shared_plugin_paths"):
            cmd += ["--disable-slash-commands"]
        cmd += history_flags
        cmd += native_flags or ['--settings', '{"disableAllHooks":true}']
        if self.cfg["claude"].get("debug"):
            cmd += ["--debug-file", str(workspace / "control" / "cli-debug.log")]
        cmd = self.sandbox_command(cmd, session, env)
        self.launch(session, cmd, env, logs, len(system) + len(user))

    def launch(self, session, cmd, env, logs, prompt_chars):
        workspace = Path(session['workspace'])
        stdout, stderr = logs / "output.json", logs / "stderr.txt"
        with self.engine.store.tx() as tx:
            current = tx.get("sessions", session["id"])
            current.update(stdout=str(stdout), stderr=str(stderr), prompt_chars=prompt_chars)
            for key in ("history_parent", "history_transcript", "history_mode"):
                if key in session:
                    current[key] = session[key]
            tx.put("sessions", current)
        cmd = [sys.executable, str(self.engine.root / "labcore" / "sessionexec.py"), "--db", str(self.engine.store.path),
               "--session", session["id"], "--"] + cmd
        with stdout.open("w") as out, stderr.open("w") as err:
            proc = subprocess.Popen(cmd, cwd=workspace, env=env, stdout=out, stderr=err,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        self.processes[session["id"]] = proc

    def terminate(self, session, *, hard=False):
        if session.get("pid") and session.get("identity") and identity(session["pid"]) == session["identity"]:
            try:
                os.killpg(session["pid"], signal.SIGKILL if hard else signal.SIGTERM)
            except ProcessLookupError:
                pass

    def poll(self):
        for session in self.engine.store.all("sessions"):
            if session["status"] not in ("running", "stopping"):
                continue
            if not session.get("pid"):
                if time.time() - session["created"] > 30:
                    self.fail(session, "Runtime restarted before session launch", retry=True)
                continue
            proc = self.processes.get(session["id"])
            running = proc.poll() is None if proc else identity(session["pid"]) == session.get("identity")
            elapsed = time.time() - session.get("started", session["created"])
            if running:
                if elapsed >= session["minutes"] * 60 or session.get("preempted"):
                    if session["status"] != "stopping":
                        atomic_json(Path(session["workspace"]) / "control" / "PREEMPT.json",
                                    {"reason": session.get("stop_reason", "Session time allowance reached"), "checkpoint_now": True})
                        with self.engine.store.tx() as tx:
                            session.update(status="stopping", stop_at=time.time() + self.cfg["scheduling"]["preemption_grace_seconds"])
                            tx.put("sessions", session)
                    elif time.time() >= session["stop_at"]:
                        self.terminate(session, hard=True)
                continue
            self.processes.pop(session["id"], None)
            text = Path(session["stdout"]).read_text(errors="replace")
            stderr = Path(session["stderr"]).read_text(errors="replace")
            envelope, usage = parse_session_output(self.engine.store, session, text)
            atomic_json(Path(session["workspace"]) / "control" / "last-result.json", envelope or {"partial_output": text[-20000:]})
            message = str(envelope.get("result", "")) + "\n" + stderr
            with self.engine.store.tx() as tx:
                current = tx.get("sessions", session["id"])
                current.update(seconds=max(1, int(elapsed)), usage=usage)
                tx.put("sessions", current)
            if envelope.get('is_error') and re.search(r'failed to authenticate|oauth.{0,50}(?:expired|revoked)|not logged in|please run /login|invalid authentication credentials', message, re.I):
                self.fail(session, 'Claude CLI login expired or unavailable; sign in with claude auth login', auth_required=True)
                continue
            quota = re.search(r"(?:hit|reached|exceeded).{0,35}(?:usage|limit)|usage.?limit|rate.?limit|out of.{0,15}usage", message, re.I)
            if quota and envelope.get("is_error", True):
                until, exact = reset_time(message, self.cfg, envelope=envelope)
                with self.engine.store.tx() as tx:
                    old = tx.get("settings", accounts.key("cooldown", session.get("account")), {})
                    if not exact and old.get("until", 0) > time.time():
                        until, exact = old["until"], old.get("exact", False)
                    tx.put("settings", {"id": accounts.key("cooldown", session.get("account")), "until": until, "exact": exact, "reason": message[:500]})
                self.fail(session, "Subscription/model usage limit; saved work will resume after cooldown", retry=True, quota=True)
                self.cooldown_notice(session, until, exact)
                continue
            if session.get("preempted"):
                self.fail(session, "Preempted for higher-priority work; resume from checkpoint", retry=True, preempted=True)
                continue
            try:
                if envelope.get("is_error") or not envelope:
                    raise ValueError(message[:800] or "No valid Claude result; inspect the session stderr")
                result = envelope.get("structured_output")
                if result is None:
                    result = json.loads(envelope.get("result", ""))
                self.engine.apply_result(session["id"], result, usage)
            except (ValueError, KeyError, TypeError) as e:
                repairable = bool(envelope and not envelope.get("is_error"))
                self.fail(session, str(e), retry=repairable)

    def fail(self, session, reason, *, retry=False, quota=False, preempted=False, auth_required=False):
        retry = retry or auth_required
        with self.engine.store.tx() as tx:
            if auth_required:
                state = tx.get('settings', accounts.key('authentication', session.get('account')), {})
                if not state.get('required'):
                    tx.put('settings', dict(id=accounts.key('authentication', session.get('account')), required=True, since=time.time(),
                                           source_mtime_ns=auth.source_state(accounts.directory(self.cfg, session['account']) / '.credentials.json' if session.get('account') else None)['mtime_ns']))
            current = tx.get("sessions", session["id"])
            current.update(status="auth" if auth_required else "quota" if quota else "interrupted" if preempted else "failed", error=reason,
                           seconds=current.get("seconds", max(1, int(time.time() - current["created"]))), finished=time.time())
            tx.put("sessions", current)
            for request_id in current.get("request_ids", []):
                req = tx.get("requests", request_id)
                if req["status"] == "cancelled":
                    continue
                req["last_error"] = reason
                req["attempts"] = req.get("attempts", 0) + (0 if quota or preempted or auth_required else 1)
                req["status"] = "queued" if retry and req["attempts"] <= self.cfg["scheduling"]["max_error_retries"] or quota or preempted or auth_required else "blocked"
                tx.put("requests", req)
            task = tx.get("tasks", current.get("task", ""))
            if task and task["status"] != "cancelled":
                task["error_retries"] = task.get("error_retries", 0) + (0 if quota or preempted or auth_required else 1)
                allowed_retry = retry and (quota or preempted or auth_required or task["error_retries"] <= self.cfg["scheduling"]["max_error_retries"])
                task.update(status="queued" if allowed_retry else "blocked", last_error=reason)
                tx.put("tasks", task)
                if not quota and not preempted and not allowed_retry:
                    self.engine.notify_supervisor(tx, task, "Specialist session stopped: " + reason, key="failure:" + session["id"])
            elif not quota and not preempted and (not retry or any(
                    tx.get("requests", request_id, {}).get("status") == "blocked" for request_id in current.get("request_ids", []))):
                key = "failure-notice:" + session["id"]
                tx.put("outbox", {"id": key, "channel": session["channel"], "thread": session.get("thread"),
                                  "text": "I couldn’t finish this response. Your message and any partial work are saved. "
                                          "The runtime needs attention before retrying; see `python lab.py doctor`.",
                                  "status": "queued", "urgent": False, "created": time.time()})
            tx.event("session_stopped", session=session["id"], reason=reason)
        if auth_required:
            auth.notice(self.engine, session['channel'], session.get('thread'), session.get('account'))

    def cooldown_notice(self, session, until, exact):
        if session.get("account"):
            from . import subscription
            if not subscription.gate(self.engine, session["model"], "supervisor"):
                return
        zone = ZoneInfo(self.cfg.get("timezone", "UTC"))
        when = dt.datetime.fromtimestamp(until, zone).strftime("%b %d, %I:%M %p %Z")
        text = ("Claude’s usage allowance is currently unavailable. I’ve saved our place and queued unfinished work. "
                + (f"I’ll resume after the reported reset around {when}." if exact else
                   f"No reset time was supplied, so I’ll check again around {when}; that is an estimate."))
        self.engine.store.outbox(session["channel"], session.get("thread"), text,
                                 key=f"cooldown:{int(until)}:{session['channel']}:{session.get('thread')}")
