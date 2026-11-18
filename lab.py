#!/usr/bin/env python3
"""Research Lab v2: operator commands and sandbox-local worker helpers."""
import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Landlock grants the package and this file, but deliberately not a listing of
# the repository root. Load the known package directly rather than widening it.
if "labcore" not in sys.modules:
    import importlib.util
    package = Path(__file__).resolve().parent / "labcore"
    spec = importlib.util.spec_from_file_location("labcore", package / "__init__.py",
                                                 submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["labcore"] = module
    spec.loader.exec_module(module)

from labcore import __version__
from labcore.config import ROOT, load
from labcore.files import atomic_json, slug
from labcore.store import uid


def workspace():
    value = os.environ.get("LAB_WORKSPACE")
    if not value or not os.environ.get("LAB_SESSION"):
        raise ValueError("This command runs inside an authorized specialist session")
    return Path(value)


def main():
    parser = argparse.ArgumentParser(description="Supervisor-led research lab")
    sub = parser.add_subparsers(dest="command", required=True)
    dashboard = sub.add_parser("dashboard", help="serve the local read-only research dashboard")
    dashboard.add_argument("--port", type=int, default=8765)
    review = sub.add_parser("story-review", help="inspect or explicitly retry Research story editorial reviews")
    review.add_argument("--project", help="project ID; required for retry")
    review.add_argument("--retry", action="store_true")
    sub.add_parser("init")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--slack", action="store_true")
    sub.add_parser("status")
    sub.add_parser("usage")
    account = sub.add_parser('subscription', help='account-wide usage percentages and reset times')
    account.add_argument('--refresh', action='store_true', help='read account metadata without a model call')
    sub.add_parser('schedule', help='inspect durable supervisor wake-ups')
    sub.add_parser("resume-account", help="clear provider cooldown only after your allowance is actually available")
    job = sub.add_parser("job")
    job.add_argument("action", choices=["start", "status"])
    job.add_argument("--name", default="Experiment")
    job.add_argument("--hours", type=float)
    job.add_argument("--gpus", type=int, default=0)
    job.add_argument("--gpu-mode", choices=("exclusive", "shared"), default="exclusive")
    job.add_argument("--gpu-memory-mib", type=int, help="Peak memory reservation per GPU for shared jobs (MiB)")
    job.add_argument("--cpu-threads", type=int)
    job.add_argument("--metrics")
    job.add_argument("--seed", type=int, action="append", default=[])
    job.add_argument("--config-file", action="append", default=[])
    work = sub.add_parser("work")
    work.add_argument("action", choices=["status", "checkpoint"])
    work.add_argument("--completed", action="append", default=[])
    work.add_argument("--next-step", default="")
    work.add_argument("--blocked", default="")
    work.add_argument("--file", action="append", default=[])
    library = sub.add_parser("library")
    library.add_argument("action", choices=["search", "show"])
    library.add_argument("query")
    argv = sys.argv[1:]
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    command = argv[split + 1:]
    try:
        if args.command == "work":
            base = workspace()
            if args.action == "checkpoint":
                atomic_json(base / "checkpoint.json", {"completed": args.completed, "next_step": args.next_step,
                             "blocked": args.blocked, "files": args.file, "saved_at": time.time()})
                print("Checkpoint saved.")
            else:
                data = json.loads((base / "control" / "status.json").read_text())
                print(json.dumps({"project": {k: (data.get("project") or {}).get(k) for k in ("name", "status", "daily_minutes")},
                    "budget_usage": data.get("budget_usage"),
                    "launch_holds": [{k: t.get(k) for k in ("id", "goal", "status", "launch_hold")}
                                     for t in data.get("tasks", []) if t.get("launch_hold")],
                    "task": {k: (data.get("task") or {}).get(k) for k in ("goal", "status", "notes", "launch_hold")},
                    "jobs": [{k: j.get(k) for k in ("id", "name", "status", "error", "run_dir")} for j in data.get("jobs", [])],
                    "children": [{k: t.get(k) for k in ("id", "goal", "status", "report")} for t in data.get("children", [])],
                    "allocation": {k: (data.get("root_allocation") or {}).get(k) for k in ("budget", "resources")},
                    "preempt_requested": (base / "control" / "PREEMPT.json").exists()}, indent=2))
            return
        if args.command == "job":
            base = workspace()
            if args.action == "status":
                for path in sorted((base / "control").glob("job-*.receipt.json")):
                    print(path.read_text())
                status = json.loads((base / "control" / "status.json").read_text())
                print(json.dumps(status.get("jobs", []), indent=2))
            else:
                if not command or args.hours is None:
                    raise ValueError("Usage: lab.py job start --hours H [--gpus N] -- COMMAND")
                request = {"name": args.name, "hours": args.hours, "gpus": args.gpus, "gpu_mode": args.gpu_mode, "gpu_memory_mib": args.gpu_memory_mib, "cpu_threads": args.cpu_threads,
                           "command": command, "cwd": str(Path.cwd()), "metrics": args.metrics,
                           "seeds": args.seed, "configs": args.config_file}
                path = base / "control" / ("job-" + uid() + ".json")
                atomic_json(path, request)
                print("Experiment request saved. The runtime will validate its budget and write a receipt in control/.")
            return
        if args.command == "library":
            for path in (ROOT / "library" / "papers").glob("*/note.json"):
                note = json.loads(path.read_text())
                if args.query.lower() in json.dumps(note).lower():
                    print(json.dumps(note, indent=2) if args.action == "show" else
                          f"{note['canonical_id']} | {note['title']} | depth: {note['read_depth']} | {path}")
            return
        if args.command == "dashboard":
            from labcore.dashboard import serve
            serve(ROOT, args.port)
            return
        if args.command == 'story-review':
            from labcore.store import Store
            store = Store(ROOT/'runtime/lab.sqlite3')
            if args.retry:
                if not args.project or not store.get('projects', args.project):
                    raise ValueError('Retry requires --project with a known project ID')
                with store.tx() as tx:
                    state = tx.get('story_review_states', args.project, {})
                    if state.get('status') == 'running':
                        raise ValueError('This project already has a running review')
                    state.update(id=args.project, status='retry_requested', basis=None, candidate=None,
                                 candidate_since=0, last_started=0, message='Explicit review retry requested.')
                    tx.put('story_review_states', state)
            for state in store.all('story_review_states'):
                if not args.project or state['id'] == args.project:
                    print(json.dumps(state, indent=2))
            return
        cfg = load()
        if args.command == "doctor":
            checks = {"platform_version": __version__, "claude": shutil.which(cfg["claude"]["bin"]),
                      "taskset": shutil.which("taskset"), "supervisor_image": (ROOT / cfg["slack"]["avatar_file"]).exists(),
                      "subscription_credentials_present": (Path.home() / ".claude" / ".credentials.json").exists()}
            from labcore.auth import source_state
            from labcore import accounts
            if accounts.profiles(cfg):
                checks['accounts'] = {a['id']: source_state(accounts.directory(cfg, a['id']) / '.credentials.json')['usable'] for a in accounts.profiles(cfg)}
                checks['subscription_login_locally_usable'] = any(checks['accounts'].values())
            else:
                checks['subscription_login_locally_usable'] = source_state()['usable']
            if not checks['subscription_login_locally_usable']:
                checks['subscription_login_action'] = 'Run claude auth login on the server; a credentials file alone does not mean the CLI is signed in.'
            libc = ctypes.CDLL(None, use_errno=True)
            checks["landlock_abi"] = libc.syscall(444, 0, 0, 1)
            health_file = ROOT / 'runtime/daemon.json'
            if health_file.exists():
                from labcore.runner import identity
                health = json.loads(health_file.read_text())
                checks['daemon_running'] = bool(health.get('status') == 'running' and health.get('identity') and identity(health.get('pid')) == health.get('identity'))
            if checks["claude"]:
                checks["claude_version"] = subprocess.run([checks["claude"], "--version"], capture_output=True, text=True).stdout.strip()
            if args.slack:
                from slack_sdk import WebClient
                from labcore.config import secrets
                bot, _ = secrets(ROOT)
                result = WebClient(token=bot).auth_test()
                checks["slack_connected"] = bool(result["ok"])
                checks["bot_user"] = result.get("user_id")
                granted = {s.strip() for s in result.headers.get("x-oauth-scopes", "").split(",")}
                if granted != {""}:
                    checks["slack_missing_scopes"] = sorted({"chat:write", "reactions:write", "channels:read", "groups:read", "files:read", "files:write"} - granted)
            print(json.dumps(checks, indent=2))
            return
        from labcore.engine import Engine
        engine = Engine(cfg)
        if args.command == 'subscription':
            from labcore.subscription import Monitor, display
            if args.refresh:
                Monitor(engine).refresh(force=True)
            print(display(engine))
        elif args.command == 'schedule':
            print(json.dumps(engine.store.all('wakeups'), indent=2))
        elif args.command == "init":
            print("Research Lab v2 initialized. Invite the configured Slack app to a new channel, then run python labd.py.")
        elif args.command == "resume-account":
            with engine.store.tx() as tx:
                from labcore import accounts
                for account in ([a["id"] for a in accounts.profiles(cfg)] or [None]):
                    tx.put("settings", {"id": accounts.key("cooldown", account), "until": 0, "exact": False})
            print("Account cooldown cleared; saved work can resume when the daemon is running.")
        elif args.command == "status":
            print(json.dumps({"projects": engine.store.all("projects"), "tasks": engine.store.all("tasks"),
                              "cooldown": engine.store.get("settings", "cooldown", {}),
                              "runtime_error": engine.store.get("settings", "runtime_error", {})}, indent=2))
        elif args.command == "usage":
            sessions = engine.store.all("sessions")
            tasks = engine.store.all("tasks")
            print(json.dumps({"sessions": len(sessions), "agent_minutes": round(sum(s.get("seconds", 0) for s in sessions) / 60, 2),
                              "reported_output_tokens": sum(s.get("usage", {}).get("output_tokens", 0) or 0 for s in sessions),
                              "accepted_deliverables": sum(t["status"] == "accepted" and not t.get("parent") and not t.get("meeting") and t["role"] in ("engineer", "literature") for t in tasks),
                              "accepted_verifications": sum(t["status"] == "accepted" and t["role"] == "verifier" for t in tasks),
                              "completed_workers": sum(t["status"] == "accepted" and bool(t.get("parent")) for t in tasks),
                              "note": "Reported usage is not the remaining Claude subscription allowance."}, indent=2))
    except (ValueError, OSError, KeyError) as e:
        parser.exit(1, str(e) + "\n")


if __name__ == "__main__":
    main()
