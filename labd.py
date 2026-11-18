#!/usr/bin/env python3
"""Multi-channel research supervisor daemon. Idle operation makes no model calls."""
import argparse
import fcntl
import signal
import time

from labcore.config import ROOT, load, secrets
from labcore.engine import Engine
from labcore.runtime import Runtime
from labcore.files import atomic_json
from labcore.runner import identity
import os


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-slack", action="store_true")
    parser.add_argument("--once", action="store_true", help="one scheduling tick, useful for diagnostics")
    args = parser.parse_args()
    cfg = load()
    engine = Engine(cfg)
    lock = (ROOT / "runtime" / "daemon.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(1, "A research-lab daemon is already running.\n")
    from labcore.customization import import_memory
    import_memory(engine)
    runtime = Runtime(engine)
    bridge = handler = None
    if not args.no_slack:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        from labcore.slack import SlackBridge
        bot, app_token = secrets(ROOT)
        app = App(token=bot)
        bridge = SlackBridge(engine, runtime, app.client)

        @app.event("message")
        def message(event):
            bridge.message(event)

        @app.event("member_joined_channel")
        def joined(event):
            bridge.joined(event)

        @app.event("channel_archive")
        @app.event("channel_deleted")
        @app.event("group_archive")
        @app.event("group_deleted")
        def removed(event):
            bridge.archived(event)

        @app.action("approve_job")
        @app.action("deny_job")
        def approval(ack, body):
            ack()
            bridge.approval(body)

        handler = SocketModeHandler(app, app_token)
        handler.connect()
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print("Research Lab v2 running. Specialists start only with authorized work. Ctrl+C stops the daemon.", flush=True)
    health = {"pid": os.getpid(), "identity": identity(os.getpid()), "started": time.time(), "status": "running"}
    atomic_json(ROOT / "runtime" / "daemon.json", health)
    try:
        while running:
            if bridge:
                try:
                    bridge.discover()
                    bridge.deliver()
                except Exception as e:
                    print(f"Slack connection: {type(e).__name__}; will retry.", flush=True)
            runtime.tick()
            if bridge:
                bridge.activity.sync()
            if args.once:
                break
            time.sleep(cfg["scheduling"]["poll_seconds"])
    finally:
        atomic_json(ROOT / "runtime" / "daemon.json", dict(health, status="stopped", stopped=time.time()))
        if handler:
            handler.close()
        # Active model/job processes retain durable logs; the next daemon adopts them.
        lock.close()


if __name__ == "__main__":
    main()
