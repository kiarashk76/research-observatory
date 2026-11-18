"""Landlock filesystem wrapper. Read grants never imply write grants.

Landlock is defense in depth, not a container/network/account-quota sandbox. The
available kernel ABI determines coverage (see README). Fail closed if unavailable.
"""
import argparse
import ctypes
import errno
import os
from pathlib import Path


def restrict(read_paths, write_paths, gpu_process=False):
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 1:
        raise OSError(ctypes.get_errno(), "Landlock is unavailable; refusing an unconfined session")
    # EXECUTE through MAKE_SYM; REFER (v2), TRUNCATE (v3).
    handled = (1 << 13) - 1
    if abi >= 2:
        handled |= 1 << 13
    if abi >= 3:
        handled |= 1 << 14

    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class Beneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    attr = Ruleset(handled)
    ruleset = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "Cannot create Landlock ruleset")
    try:
        grants = [(p, None) for p in read_paths] + [(p, handled) for p in write_paths]
        if gpu_process:
            # Landlock has no filename glob rules. Permit only write/truncate
            # under THIS process's task directory; never /proc or other PIDs.
            # The directory rule covers threads created after exec. Forked
            # children get different task directories and remain restricted.
            grants.append((f"/proc/{os.getpid()}/task", (1 << 1) | ((1 << 14) if abi >= 3 else 0)))
        for name, write_access in grants:
            path = Path(name).resolve()
            if not path.exists():
                continue
            access = write_access if write_access is not None else (1 | (1 << 2) | (1 << 3))
            if not path.is_dir():
                access &= 1 | (1 << 1) | (1 << 2) | (1 << 14)
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = Beneath(access, fd)
                if libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0) != 0:
                    raise OSError(ctypes.get_errno(), f"Cannot grant sandbox path {path}")
            finally:
                os.close(fd)
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, ruleset, 0):
            raise OSError(ctypes.get_errno(), "Cannot enforce Landlock")
    finally:
        os.close(ruleset)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--read", action="append", default=[])
    parser.add_argument("--write", action="append", default=[])
    parser.add_argument("--gpu-process", action="store_true", help="Allow GPU process writes in its own procfs thread directory")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    restrict(args.read, args.write, args.gpu_process)
    os.execvpe(cmd[0], cmd, os.environ)


if __name__ == "__main__":
    main()
