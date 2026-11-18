"""Multi-channel Slack adapter. Normal replies always stay in their source thread."""
import json
import re
import time
import urllib.request
import uuid
import datetime as dt
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlparse

from .files import atomic_json, project_layout
from . import subscription, auth
from .activity import Activity
from .slack_format import message_text
from .slack_input import incoming_text


class PrivateFileRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        redirected = super().redirect_request(request, fp, code, message, headers, newurl)
        if redirected and urlparse(newurl).hostname != urlparse(request.full_url).hostname:
            redirected.remove_header("Authorization")
        return redirected


class SlackBridge:
    def __init__(self, engine, runtime, client, *, bot_user=None):
        self.engine, self.runtime, self.client = engine, runtime, client
        self.bot_user = bot_user or client.auth_test()["user_id"]
        self.next_discovery = 0
        self.activity = Activity(engine, client)

    def allowed(self, user):
        allow = self.engine.cfg["slack"].get("allowed_user_ids", [])
        return not allow or user in allow

    def enroll(self, channel_id):
        info = self.client.conversations_info(channel=channel_id)["channel"]
        return self.engine.enroll(channel_id, info.get("name", channel_id),
                                  info.get("topic", {}).get("value", ""), info.get("purpose", {}).get("value", ""))

    def joined(self, event):
        if event.get("user") == self.bot_user:
            self.enroll(event["channel"])

    def discover(self):
        if time.time() < self.next_discovery:
            return
        self.next_discovery = time.time() + self.engine.cfg["slack"].get("poll_joined_channels_seconds", 300)
        cursor = None
        while True:
            result = self.client.users_conversations(types="public_channel,private_channel", limit=200,
                                                     exclude_archived=True, **({"cursor": cursor} if cursor else {}))
            for channel in result.get("channels", []):
                if not self.engine.store.get("channels", channel["id"]):
                    self.engine.enroll(channel["id"], channel.get("name", channel["id"]),
                                       channel.get("topic", {}).get("value", ""), channel.get("purpose", {}).get("value", ""))
            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

    def archived(self, event):
        channel_id = event.get("channel")
        with self.engine.store.tx() as tx:
            channel = tx.get("channels", channel_id)
            if not channel:
                return
            channel["active"] = False
            tx.put("channels", channel)
            if not any(c["project"] == channel["project"] and c["active"] for c in tx.all("channels")):
                project = tx.get("projects", channel["project"])
                project["status"] = "paused"
                tx.put("projects", project)

    def message(self, event):
        if event.get("bot_id") or event.get("user") == self.bot_user or event.get("subtype") not in (None, "file_share", "thread_broadcast"):
            return
        if not event.get("channel") or not self.allowed(event.get("user")):
            return
        channel_id, timestamp = event["channel"], event["ts"]
        thread = event.get("thread_ts") or timestamp
        channel = self.engine.store.get("channels", channel_id) or self.enroll(channel_id)
        text = incoming_text(event)
        text = re.sub(r"<@" + re.escape(self.bot_user) + r">\s*", "", text).strip()
        if self.command(channel, thread, text, timestamp, user=event.get("user")):
            return
        attachments = self.attachments(channel, event.get("files", []))
        if attachments:
            text += "\nAttached files:\n" + "\n".join(attachments)
        if not text:
            return
        self.engine.receive(channel_id, timestamp, thread, event.get("user", "human"), text)
        auth.notice(self.engine, channel_id, thread)
        self.activity.sync()
        cooldown = self.engine.store.get("settings", "cooldown", {})
        if not self.engine.cfg["claude"].get("accounts") and cooldown.get("until", 0) > time.time():
            self.runtime.runner.cooldown_notice({"channel": channel_id, "thread": thread}, cooldown["until"], cooldown.get("exact", False))

    def attachments(self, channel, files):
        if not files:
            return []
        project = self.engine.store.get("projects", channel["project"])
        inbox = project_layout(self.engine.root, project) / "inbox"
        inbox.mkdir(exist_ok=True)
        results = []
        for file in files[:5]:
            url = file.get("url_private_download") or file.get("url_private")
            host = urlparse(url or "").hostname or ""
            if not url or not url.startswith("https://") or not (host == "files.slack.com" or host.endswith(".slack.com")):
                continue
            if file.get("size", 0) > 20 * 1024 * 1024:
                results.append(file.get("name", "Attachment") + " exceeds the 20 MiB download limit; ask for a relevant excerpt.")
                continue
            path = inbox / (re.sub(r"\W", "", file["id"]) + "-" + Path(file.get("name", "attachment")).name)
            if not path.exists():
                # Only Slack's private file host receives the bot token.
                request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.client.token})
                try:
                    with urllib.request.build_opener(PrivateFileRedirect()).open(request, timeout=20) as response:
                        data = response.read(20 * 1024 * 1024 + 1)
                except OSError:
                    results.append(file.get("name", "Attachment") + " could not be downloaded. Check files:read scope or provide a source link.")
                    continue
                if len(data) > 20 * 1024 * 1024:
                    continue
                path.write_bytes(data)
            results.append(str(path))
        return results

    def command(self, channel, thread, text, timestamp, user=None):
        if text.lower().startswith('!notes allow '):
            from . import research_notes
            key='notes-permission:'+channel['id']+':'+timestamp
            if self.engine.store.get('commands',key):return True
            try:
                with self.engine.store.tx() as tx:
                    if tx.get('commands',key):return True
                    reference=text[len('!notes allow '):].strip()
                    if ' | ' in reference:
                        name,scope=reference.split(' | ',1)
                        matches=[n for n in research_notes.notebooks(tx,channel['project']) if n['id']==name.strip() or n['title']==name.strip()]
                        if len(matches)!=1:raise ValueError('Use an unambiguous note title or ID: !notes allow NOTE | SECTION (or all)')
                        ident=matches[0]['id']
                    else:
                        ident=research_notes.target(tx,channel['project'])
                        scope=reference
                    note=research_notes.current(tx,ident)
                    permission=research_notes.grant(tx,ident,note['revision'],scope,author=user or 'slack-human')
                    tx.put('commands',dict(id=key,text=text))
                reply=f"One-time Research Notes permission: `{permission['id']}` for *{permission['section_title']}*, revision {permission['base_revision']}, note_id `{permission['note_id']}`. Ask the supervisor to use this permission when ready. Saving another revision invalidates it."
            except ValueError as error:reply=str(error)
            self.engine.store.outbox(channel['id'],thread,reply,key=key)
            return True
        lowered = text.lower().strip().rstrip(".!?")
        commands = {"pause this project": "pause", "resume this project": "resume",
                    "close this thread": "close", "close this discussion": "close",
                    "reopen this thread": "reopen", "reopen this discussion": "reopen"}
        cmd = commands.get(lowered)
        pending = [j for j in self.engine.store.all("jobs") if j["status"] == "approval"
                   and j["channel"] == channel["id"] and j["thread"] == thread]
        if len(pending) == 1 and lowered in ("approved", "approve", "yes, run it", "yes run it", "yes, go ahead", "yes go ahead"):
            self.runtime.jobs.approve(pending[0]["id"])
            self.engine.store.outbox(channel["id"], thread, "Approved; the experiment is queued for available resources.",
                                     key="approval-response:" + pending[0]["id"])
            return True
        if text.startswith("!"):
            cmd = text[1:].split()[0].lower() if text[1:] else "help"
        if not cmd:
            return False
        key = "command:" + channel["id"] + ":" + timestamp
        if self.engine.store.get('commands', key):
            return True
        if cmd == 'subscription':
            snapshot = subscription.view(self.engine)
            self.runtime.subscription.refresh(force=snapshot['status'] != 'ok')
        with self.engine.store.tx() as tx:
            if tx.get("commands", key):
                return True
            tx.put("commands", {"id": key, "text": text})
            project = tx.get("projects", channel["project"])
            if cmd in ("pause", "resume"):
                project["status"] = "paused" if cmd == "pause" else "active"
                tx.put("projects", project)
                reply = ("I’ve paused new work in this project. Running experiments may finish; ask me to stop them if needed."
                         if cmd == "pause" else "This project is active again. Authorized queued work can resume.")
            elif cmd in ("close", "reopen"):
                close_key = channel["id"] + ":" + thread
                if cmd == "close":
                    tx.put("closed_threads", {"id": close_key, "time": time.time()})
                    for task in tx.all("tasks"):
                        if task["project"] == project["id"] and task["thread"] == thread and task["status"] not in ("accepted", "cancelled"):
                            task["status"] = "cancelled"
                            tx.put("tasks", task)
                    reply = "I’ve closed this discussion and cancelled its unfinished assignments. Its records are preserved."
                else:
                    tx.conn.execute("DELETE FROM records WHERE kind='closed_threads' AND id=?", (close_key,))
                    reply = "This discussion is open again. Previously cancelled assignments remain cancelled."
            elif cmd == 'subscription':
                reply = subscription.display(self.engine)
            elif cmd == 'schedule':
                zone = ZoneInfo(self.engine.cfg.get('timezone', 'UTC'))
                planned = [w for w in tx.all('wakeups') if w['project'] == project['id'] and w['status'] == 'pending']
                reply = '\n'.join('• ' + w['reason'] + ': ' + dt.datetime.fromtimestamp(w['at'], zone).strftime('%b %d, %I:%M %p %Z') for w in planned) or 'No supervisor reviews are scheduled.'
            elif cmd in ("status", "tasks", "jobs", "usage"):
                tasks = [t for t in tx.all("tasks") if t["project"] == project["id"] and t["status"] != "cancelled"]
                lines = [f"{project['name']}: {project['status']} ({project['mode']})"]
                lines += [f"• {t['goal']}: {t['status'].replace('_', ' ')}" for t in tasks[-12:]]
                sessions = [s for s in tx.all("sessions") if s["project"] == project["id"]]
                lines.append(f"Reported output tokens: {sum(s.get('usage', {}).get('output_tokens', 0) or 0 for s in sessions):,}")
                reply = "\n".join(lines)
            elif cmd == "retry":
                for req in tx.all("requests"):
                    if req["project"] == project["id"] and req["thread"] == thread and req["status"] == "blocked":
                        req.update(status="queued", attempts=0)
                        tx.put("requests", req)
                reply = "I’ve queued the blocked response for another attempt. Any account cooldown still applies."
            else:
                reply = "Talk to me naturally. Optional controls: !status, !tasks, !jobs, !usage, !subscription, !schedule, !pause, !resume, !close, !reopen, !retry."
            tx.put("outbox", {"id": key, "channel": channel["id"], "thread": thread, "text": reply,
                              "status": "queued", "urgent": False, "created": time.time()})
        return True

    def approval(self, body):
        if not self.allowed(body.get("user", {}).get("id")):
            return
        action = body["actions"][0]
        job = self.engine.store.get("jobs", action["value"])
        if not job:
            return
        try:
            self.runtime.jobs.approve(job["id"], action["action_id"] == "approve_job")
            self.engine.store.outbox(job["channel"], job["thread"], "Approved; the experiment will start when its allocated resources are available."
                                    if action["action_id"] == "approve_job" else "I’ve cancelled that experiment request.",
                                    key="approval-response:" + job["id"])
        except ValueError:
            pass  # A repeated Slack action is an idempotent acknowledgement.

    def deliver(self):
        for record in self.engine.store.all("outbox"):
            if record["status"] == "sent" or record.get("retry_after", 0) > time.time():
                continue
            try:
                if record.get("file"):
                    response = self.client.files_upload_v2(channel=record["channel"], thread_ts=record.get("thread"),
                        file=record["file"], title=record.get("title", "Research evidence"))
                    ts = None
                else:
                    kwargs = {"channel": record["channel"], "text": message_text(record["text"]), "mrkdwn": True, "unfurl_links": False,
                              "unfurl_media": False, "client_msg_id": str(uuid.uuid5(uuid.NAMESPACE_URL, record["id"]))}
                    if record.get("thread"):
                        kwargs["thread_ts"] = record["thread"]
                        kwargs["reply_broadcast"] = bool(record.get("urgent") and self.engine.cfg["slack"].get("urgent_broadcasts"))
                    if record.get("blocks"):
                        kwargs["blocks"] = record["blocks"]
                    response = self.client.chat_postMessage(**kwargs)
                    ts = response.get("ts")
                with self.engine.store.tx() as tx:
                    current = tx.get("outbox", record["id"])
                    current.update(status="sent", timestamp=ts)
                    tx.put("outbox", current)
                    if ts and record.get("text"):
                        channel = tx.get("channels", record["channel"])
                        tx.put("messages", {"id": record["channel"] + ":" + ts, "project": channel["project"],
                                            "channel": record["channel"], "thread": record.get("thread") or ts,
                                            "user": "supervisor", "text": record["text"], "time": ts, "created": time.time()})
            except Exception as e:
                response = getattr(e, "response", {})
                error = response.get("error", "") if hasattr(response, "get") else ""
                headers = getattr(response, "headers", {})
                with self.engine.store.tx() as tx:
                    current = tx.get("outbox", record["id"])
                    current.update(retry_after=time.time() + float(headers.get("Retry-After", 30)), error=error or type(e).__name__)
                    if error in ("channel_not_found", "is_archived", "not_in_channel"):
                        current["status"] = "sent"  # No endless publication to a removed channel.
                    tx.put("outbox", current)
                if error in ("channel_not_found", "is_archived", "not_in_channel"):
                    self.archived({"channel": record["channel"]})
