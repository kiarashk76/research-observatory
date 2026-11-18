"""Opt-in: one small Haiku subscription request, no Slack or research work."""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labcore.config import load, ROOT
from labcore.engine import Engine
from labcore.runtime import Runtime


def main():
    with tempfile.TemporaryDirectory(prefix="research-live-check-") as folder:
        root = Path(folder)
        for name in ("labcore", "prompts"):
            shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "lab.py", root / "lab.py")
        cfg = load()
        cfg["root"] = str(root)
        cfg["claude"].update(supervisor_model="haiku", supervisor_effort="low", supervisor_minutes=1, debug=True,
                             api_retries=0, api_timeout_ms=20000)
        cfg["scheduling"].update(debounce_seconds=0)
        cfg['subscription'] = {'enabled': False} # this test validates one model request, not account metadata polling
        engine = Engine(cfg)
        runtime = Runtime(engine)
        engine.enroll("SMOKE", "connection-check", purpose="One small connection test, not a research project")
        with engine.store.tx() as tx:
            req = tx.get("requests", "onboarding:SMOKE")
            req["status"] = "done"
            tx.put("requests", req)
        engine.receive("SMOKE", "1.1", "1.1", "human", "Connectivity test only. Reply Ready. Do not use tools or assign any work. Return no actions and no evidence.")
        started = time.time()
        try:
            while time.time() - started < 150:
                runtime.tick()
                sessions = engine.store.all("sessions")
                if sessions and sessions[-1]["status"] not in ("running", "stopping"):
                    current = sessions[-1]
                    print(json.dumps({"status": current["status"], "error": current.get("error"),
                                      "usage": current.get("usage"), "runtime_error": runtime.last_error,
                                      "elapsed_seconds": round(time.time() - started, 1)}, indent=2))
                    if current["status"] != "done" and current.get("stderr"):
                        print(Path(current["stderr"]).read_text()[-3000:])
                    return 0 if current["status"] == "done" else 1
                time.sleep(.2)
            print("Live check timed out; no additional requests were launched.")
            for current in engine.store.all("sessions"):
                print(json.dumps({"status": current["status"], "pid": current.get("pid"), "error": current.get("error")}, indent=2))
                for field in ("stderr", "stdout"):
                    if current.get(field) and Path(current[field]).exists():
                        print(field + ": " + Path(current[field]).read_text()[-2500:])
                debug = Path(current["workspace"]) / "control" / "cli-debug.log"
                if debug.exists():
                    # Print event categories only, never credential/request bodies.
                    import re
                    events = re.findall(r"\[(DEBUG|WARN|ERROR)\]\s*([^:\n]{1,90})", debug.read_text(errors="replace"))
                    print('debug_event_categories:', events[-15:])
            return 1
        finally:
            for session in engine.store.all("sessions"):
                runtime.runner.terminate(session, hard=True)
            for proc in runtime.runner.processes.values():
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
