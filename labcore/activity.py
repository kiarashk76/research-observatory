"""Visible Slack acknowledgement/activity driven by persisted request state."""
import threading
import time


class Activity:
    def __init__(self, engine, client):
        self.engine, self.client = engine, client
        self.lock = threading.Lock()

    def sync(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            self._sync()
        finally:
            self.lock.release()

    def _sync(self):
        store = self.engine.store
        if store.get('settings', 'slack_activity', {}).get('retry_after', 0) > time.time():
            return
        with store.tx() as tx:
            sessions = {s['id']: s for s in tx.all('sessions')}
            channels = {c['id']: c for c in tx.all('channels')}
            requests = {r['id']: r for r in tx.all('requests')}
            tracked = {a['id']: a for a in tx.all('activity')}
            # Only actual human messages: never invented timestamps for scheduled
            # reviews, onboarding or specialist completion notifications.
            for r in requests.values():
                if r['status'] in ('queued', 'running') and tx.get('messages', r['id']):
                    if r['id'] not in tracked:
                        message = tx.get('messages', r['id'])
                        item = dict(id=r['id'], channel=message['channel'], timestamp=message['time'], reaction='')
                        tx.put('activity', item)
                        tracked[r['id']] = item
            changes = []
            for item in tracked.values():
                if item.get('unavailable'):
                    continue
                r = requests.get(item['id'], {})
                session = sessions.get(r.get('session'), {})
                closed = tx.get('closed_threads', item['channel'] + ':' + str(r.get('thread')))
                visible = channels.get(item['channel'], {}).get('active') and not closed
                thinking = (r.get('status') == 'running' and session.get('status') == 'running'
                            and not session.get('preempted'))
                desired = ('thinking_face' if thinking else 'eyes' if r.get('status') == 'queued' else '') if visible else ''
                if item['reaction'] != desired:
                    changes.append((item, desired))
        # Network calls happen outside the database transaction. Successful state
        # persists across restarts; Slack duplicate/missing reactions are harmless.
        for item, desired in changes[:20]:
            try:
                if item['reaction']:
                    self._reaction('remove', item, item['reaction'])
                if desired:
                    self._reaction('add', item, desired)
                item.update(reaction=desired)
                with store.tx() as tx:
                    tx.put('activity', item)
                    tx.put('settings', dict(id='slack_activity', error=None, retry_after=0))
            except Exception as e:
                response = getattr(e, 'response', {})
                error = response.get('error', '') if hasattr(response, 'get') else ''
                if error in ('message_not_found', 'channel_not_found', 'is_archived', 'not_in_channel', 'no_permission'):
                    item.update(unavailable=True)
                    with store.tx() as tx:
                        tx.put('activity', item)
                    continue
                headers = getattr(response, 'headers', {})
                try:
                    retry = float(headers.get('Retry-After', 30))
                except (TypeError, ValueError):
                    retry = 30
                with store.tx() as tx:
                    tx.put('settings', dict(id='slack_activity', error=error or type(e).__name__,
                                           retry_after=time.time() + max(retry, 300 if error == 'missing_scope' else 30)))
                return  # A reaction failure must never block message delivery/research.

    def _reaction(self, action, item, name):
        try:
            getattr(self.client, 'reactions_' + action)(channel=item['channel'], timestamp=item['timestamp'], name=name)
        except Exception as e:
            response = getattr(e, 'response', {})
            error = response.get('error', '') if hasattr(response, 'get') else ''
            if error != ('already_reacted' if action == 'add' else 'no_reaction'):
                raise
