#!/usr/bin/env python3
"""Offline Claude stand-in: exercises actual process/sandbox/structured-output paths."""
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
context = json.loads(args[args.index("-p") + 1].split("\n[Context excerpt", 1)[0])
role = os.environ["LAB_ROLE"]
scenario = os.environ.get("FAKE_SCENARIO", "pipeline")
time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
result = {"reply": "", "status": "completed", "summary": "A bounded check finished", "findings": ["Sanity check passes"],
          "limitations": ["This is an offline simulation"], "evidence": [], "next_steps": [], "actions": []}
Path("report.md").write_text("Independent offline evidence.\n")
result["evidence"] = [{"path": "report.md", "description": "Check record"}]

if scenario == "quota":
    Path("checkpoint.json").write_text(json.dumps({"completed": ["Read existing evidence"], "next_step": "Continue the comparison"}))
    print(json.dumps({"is_error": True, "result": "You've hit your usage limit", "usage": {}}))
    sys.exit(0)

if role == "supervisor":
    tasks = context["tasks"]
    authors = [t for t in tasks if t["role"] == "engineer"]
    checks = [t for t in tasks if t["role"] == "verifier"]
    if not context["project"]["human_started"]:
        result.update(reply="I’m your research supervisor. What would you like to investigate here?", evidence=[])
        result["actions"] = [{"type": "set_project", "mode": "research", "purpose": "Investigate the channel's question"}]
    elif not authors:
        result["reply"] = "I’ll have the engineer check the proposed experiment, then verify the evidence."
        result["actions"] = [{"type": "assign", "role": "engineer", "goal": "Check the environment", "acceptance": "Sanity test passes",
                              "key": "check-environment", "model": "sonnet", "effort": "high", "session_minutes": .1,
                              "budget": {"minutes": 2, "sessions": 8, "children": 2, "output_tokens": 10000},
                              "resources": {"max_gpus": 0, "max_cpu_threads": 1, "total_job_hours": .1}}]
    elif authors[0]["status"] == "awaiting_review" and not checks:
        result["actions"] = [{"type": "assign", "role": "verifier", "goal": "Independently verify the environment", "acceptance": "Reproduce the sanity check",
                              "verification_target": authors[0]["id"], "model": "sonnet", "effort": "medium", "session_minutes": .1,
                              "budget": {"minutes": 1, "sessions": 2, "children": 0, "output_tokens": 10000}}]
    elif checks and checks[0]["status"] == "awaiting_review":
        result["actions"] = [{"type": "accept", "task": checks[0]["id"]}, {"type": "accept", "task": authors[0]["id"]}]
        result["reply"] = "The small check passed and was independently verified. We still need broader evaluation."
    else:
        result["reply"] = "The saved evidence is available; there is no additional work to launch."
elif role == "engineer":
    Path("sanity.py").write_text("assert 2 + 2 == 4\n")
    result["evidence"].append({"path": "sanity.py", "description": "Reproducible sanity check"})
    if scenario == "children" and not context.get("children"):
        result["status"] = "waiting_children"
        result["actions"] = [{"type": "spawn", "key": "child-" + str(i), "goal": "Check component " + str(i),
                              "acceptance": "Evidence recorded", "model": "haiku", "effort": "low", "session_minutes": .1}
                             for i in range(2)]
    elif scenario == "jobs" and not context.get("jobs"):
        request = {"name": "Offline seed", "hours": .01, "gpus": 0, "cpu_threads": 1,
                   "command": [sys.executable, "-c", "import json; open('metrics.json','w').write(json.dumps({'returns':[1,2,3]}))"],
                   "metrics": "metrics.json", "cwd": ".", "seeds": [1, 2, 3]}
        Path("control/job-offline.json").write_text(json.dumps(request))
        result["status"] = "waiting_jobs"
elif role == "verifier":
    result["verdict"] = "pass"

print(json.dumps({"is_error": False, "structured_output": result, "result": "", "num_turns": 2,
                  "usage": {"input_tokens": 50, "output_tokens": 20, "cache_read_input_tokens": 0}}))
