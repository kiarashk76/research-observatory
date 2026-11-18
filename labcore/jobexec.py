"""Experiment exit status survives daemon restarts; this monitor is outside the child sandbox."""
import argparse
import json
import subprocess
import sqlite3
import os
import signal
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exit-file", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT data FROM records WHERE kind='jobs' AND id=?", (args.job,)).fetchone()
    conn.commit()
    conn.close()
    job = json.loads(row[0]) if row else {}
    if job.get("status") != "running" or job.get("pid") != os.getpid():
        return  # Uncommitted/stale launch authorization: do not execute or overwrite a new run's exit file.
    # Keep the exit recorder alive while the child handles the group warning.
    signal.signal(signal.SIGTERM, lambda *_: None)
    try:
        code = subprocess.run(cmd).returncode
    except OSError as e:
        print(str(e), flush=True)
        code = 127
    path = Path(args.exit_file)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"exit_code": code}))
    temp.replace(path)


if __name__ == "__main__":
    main()
