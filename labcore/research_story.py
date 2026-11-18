"""Living project narrative, backed by supervisor notes and immutable revisions."""
import hashlib
import re
import time
import uuid

PATH = 'notes/research-story.md'


def table_mode(engine):
    return getattr(engine,'cfg',{}).get('research_story',{}).get('format')=='study_table'


def table_text(records):
    def cell(value):return str(value or '').replace('\n',' ').replace('|','\\|').strip()
    lines=['# Research story','', '| Study | Brief summary | Status | Paper section | Detailed report |',
           '| --- | --- | --- | --- | --- |']
    for study in records:
        link=lambda key:'[PDF]('+study[key]+')' if study.get(key) else 'Not available yet'
        state={'planned':'Planned','running':'Study ongoing','completed':'Study completed','failed':'Study failed'}.get(study.get('execution'),'Status not recorded')
        status=state+('; '+cell(study['status_note']) if study.get('status_note') else '')
        lines.append('| '+ ' | '.join((cell(study.get('title')),cell(study.get('question')),status,link('paper_pdf'),link('detailed_pdf')))+' |')
    return '\n'.join(lines)+'\n'


def sync_table(tx, project):
    """Update the generated note only when rows change; no model/review calls."""
    from .store import utc,uid
    text=table_text([s for s in tx.all('studies') if s.get('project')==project])
    key=project+':'+PATH;old=tx.get('project_notes',key,{})
    if old.get('text')==text:return old
    note=dict(id=key,project=project,path=PATH,text=text,folder=False,time=utc())
    tx.put('project_notes',note);tx.put('note_history',note,key=uid())
    return note


def story_context(engine, project):
    if table_mode(engine):
        return dict(format='study_table',path=PATH,instruction='Research story is a fixed current-study table generated from the registry. Update title, brief question/purpose, execution, status_note, paper_pdf and detailed_pdf via update_study. Replace PDF links in the same row when updated; add a row only for a new study. No older-version section, narrative, gallery or editorial review. Do not rewrite PDFs merely to update this table. Earlier narrative/style instructions are superseded.')
    note = engine.store.get('project_notes', project+':'+PATH, {})
    return dict(path=PATH, text=note.get('text', ''), updated=note.get('time'),
        instruction='Maintain this standalone research narrative after meaningful results, verification or changes of direction. Publish with write_note; include a dated What changed section and project-relative evidence links. Read the current version before editing. Use inline raster figures with explanatory captions and compact Markdown tables so this reads as a growing paper. Delegate synthesis to a literature specialist if useful, then review and publish it yourself.')


def home_message(project, story):
    """A compact Slack-native index, never a truncated copy of the paper."""
    title = 'Project home · '+project.get('name', 'Research project')
    chapters = []
    for heading in re.findall(r'^### (.+)$', story['text'], re.MULTILINE):
        if re.match(r'(?:Q[1-9]|Phase [0-9])\b', heading) and heading not in chapters:
            chapters.append(heading)
    contents = '\n'.join('• '+heading for heading in chapters[:7])
    if not contents:
        contents = 'Motivation → starting point → research questions → findings → next investigations'
    updated = str(story.get('time', 'unknown')).replace('T', ' ').replace('+00:00', ' UTC')
    blocks = [
        {'type':'header','text':{'type':'plain_text','text':title[:150],'emoji':False}},
        {'type':'section','text':{'type':'plain_text','text':'Read the living project report in the dashboard: select this project, then open Research story.','emoji':False}},
        {'type':'section','text':{'type':'plain_text','text':'IN THE REPORT\n'+contents,'emoji':False}},
        {'type':'divider'},
        {'type':'section','text':{'type':'plain_text','text':'Research story contains the figures, results tables, evidence links and earlier editions. Work and Experiments show live progress. Continue discussions in their original Slack threads.','emoji':False}},
        {'type':'context','elements':[{'type':'plain_text','text':'Report updated: '+updated,'emoji':False}]},
    ]
    text = title+'\n\nDashboard → Research story\n\n'+contents+'\n\nReport updated: '+updated
    return text, blocks


class ProjectHomes:
    """One editable, pinned Slack index per channel; no periodic digest messages."""
    def __init__(self, engine, client):
        self.engine, self.client = engine, client
        self.next_check = 0

    def sync(self):
        if time.time() < self.next_check:
            return
        self.next_check = time.time()+60
        for channel in self.engine.store.all('channels'):
            if not channel.get('active'):
                continue
            story = self.engine.store.get('project_notes', channel['project']+':'+PATH, {})
            if not story.get('text'):
                continue
            project = self.engine.store.get('projects', channel['project'], {})
            key = 'project_home:'+channel['id']
            state = self.engine.store.get('settings', key, {})
            if state.get('retry_after', 0) > time.time():
                continue
            text, blocks = home_message(project, story)
            digest = hashlib.sha256((text+story['text']).encode()).hexdigest()
            try:
                if not state.get('timestamp'):
                    result = self.client.chat_postMessage(channel=channel['id'], text=text, blocks=blocks,
                        client_msg_id=str(uuid.uuid5(uuid.NAMESPACE_URL, key)), unfurl_links=False, unfurl_media=False)
                    state['timestamp'] = result['ts']
                    state['digest'] = digest
                    # Persist before attempting the optional pin, so missing scope cannot duplicate posts.
                    with self.engine.store.tx() as tx:
                        tx.put('settings', dict(state, id=key, digest=digest))
                elif state.get('digest') != digest:
                    self.client.chat_update(channel=channel['id'], ts=state['timestamp'], text=text, blocks=blocks,
                        unfurl_links=False, unfurl_media=False)
                if not state.get('pinned'):
                    self.client.pins_add(channel=channel['id'], timestamp=state['timestamp'])
                    state['pinned'] = True
                state.update(id=key, digest=digest, error=None, retry_after=0)
            except Exception as exc:
                response = getattr(exc, 'response', {})
                error = response.get('error', type(exc).__name__)
                if error == 'already_pinned':
                    state.update(pinned=True, digest=digest)
                state.update(id=key, error=error, retry_after=time.time()+3600)
            with self.engine.store.tx() as tx:
                tx.put('settings', state)
