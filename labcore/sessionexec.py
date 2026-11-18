"""Claim a persisted session before exec: a delayed/orphan launcher cannot duplicate it."""
import argparse
import json
import os
import sqlite3
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT data FROM records WHERE kind='sessions' AND id=?", (args.session,)).fetchone()
    if not row:
        return
    session = json.loads(row[0])
    if session["status"] != "running" or session.get("pid"):
        return
    session.update(pid=os.getpid(), identity=Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19], started=time.time())
    conn.execute("UPDATE records SET data=? WHERE kind='sessions' AND id=?", (json.dumps(session), args.session))
    conn.commit()
    conn.close()
    os.execvpe(cmd[0], cmd, os.environ)


if __name__ == "__main__":
    main()
