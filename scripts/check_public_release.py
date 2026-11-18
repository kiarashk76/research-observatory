#!/usr/bin/env python3
"""Read-only checks of Git-tracked files before publishing a platform release.

This is a guardrail, not a complete secret detector or historical leak audit.
It never prints matched secret values. Run after staging the intended release.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
FORBIDDEN_DIRS={'runtime','workspaces','projects','library','archive','backups','node_modules','.aws','.ssh','.claude','.codex','.agents','.venv','venv','__pycache__'}
FORBIDDEN_FILES={'config.yaml','.slack.env','.env','credentials.json','credential-source.json','Caddyfile','daemon.json'}
TOKENS=[re.compile(rb'xox[baprs]-[A-Za-z0-9-]{20,}'),re.compile(rb'gh[pousr]_[A-Za-z0-9]{20,}'),re.compile(rb'github_pat_[A-Za-z0-9_]{30,}'),re.compile(rb'sk-ant-[A-Za-z0-9_-]{20,}'),re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')]

def main():
    proc=subprocess.run(['git','ls-files','-z'],cwd=ROOT,capture_output=True)
    if proc.returncode:
        print('Run this check inside an initialized Git repository after staging the release.');return 2
    paths=[Path(name.decode()) for name in proc.stdout.split(b'\0') if name]
    if not paths:
        print('No tracked/staged files found; stage the intended release first.');return 2
    issues=[]
    for relative in paths:
        path=ROOT/relative
        if set(relative.parts)&FORBIDDEN_DIRS or relative.name in FORBIDDEN_FILES:
            issues.append((str(relative),'private installation file/directory'))
        if relative.suffix.lower() in {'.sqlite','.sqlite3','.db','.pem','.key','.log'}:
            issues.append((str(relative),'private database/key/log file'))
        if path.is_symlink():issues.append((str(relative),'symlink requires manual publication review'));continue
        if not path.is_file():issues.append((str(relative),'tracked file missing'));continue
        body=path.read_bytes()
        if any(pattern.search(body) for pattern in TOKENS):issues.append((str(relative),'possible credential or private key'))
    if issues:
        for path,reason in issues:print(path+': '+reason)
        print('Public-release check failed; matched credential values were not printed.');return 1
    print(f'Public-release check passed for {len(paths)} tracked files. Review Git history and personal/deployment details separately.')
    return 0

if __name__=='__main__':raise SystemExit(main())
