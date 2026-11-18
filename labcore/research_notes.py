"""Human-owned project notebooks, immutable revisions and explicit agent edits."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile

from .files import scoped
from .store import Store, uid, utc

PATH = 'notes/research-notes.md'
MAX_TEXT = 500_000


class Conflict(ValueError):
    pass


def store(root):
    return Store(Path(root)/'runtime/lab.sqlite3')


def project(tx, ident):
    record=tx.get('research_notes',ident)
    value=tx.get('projects',record['project'] if record else ident)
    if not value:raise ValueError('Unknown project')
    return value


def current(tx, ident):
    owner=project(tx,ident)
    value=tx.get('research_notes',ident,dict(id=ident,project=owner['id'],revision=0,markdown='',updated=None,author='human'))
    if value.get('deleted') or value.get('archived'):raise ValueError('This note is archived or deleted')
    return {'updated':None,'author':'human',**value,'title':value.get('title','Research Notes')}


def note_path(note):
    return 'notes/research-notes/'+note['id']+'.md'


def notebooks(tx, pid):
    project(tx,pid)
    default=tx.get('research_notes',pid,{})
    return ([] if (default.get('deleted') or default.get('archived')) else [current(tx,pid)])+[dict(n,title=n.get('title','Research Notes')) for n in tx.all('research_notes') if n['project']==pid and n['id']!=pid and not n.get('deleted') and not n.get('archived')]


def archived_notebooks(tx, pid):
    # Earlier removals retained their source, so written notes remain recoverable.
    return [n for n in tx.all('research_notes') if n['project']==pid and (n.get('archived') or (n.get('deleted') and n.get('markdown','').strip()))]


def archived_note(tx,pid,ident):
    matches=[n for n in archived_notebooks(tx,pid) if n['id']==ident]
    if len(matches)!=1:raise ValueError('Archived note is unavailable in this project')
    return matches[0]


def select(tx, pid, note_id=None):
    if not tx.get('projects',pid):raise ValueError('Unknown project')
    ident=note_id or notebooks(tx,pid)[0]['id']
    record=tx.get('research_notes',ident)
    if (record and (record.get('deleted') or record.get('archived') or record['project']!=pid)) or (not record and ident!=pid):
        raise ValueError('Note is unavailable in this project')
    return ident


def target(tx,pid,note_id=None):
    if not note_id and len(notebooks(tx,pid))>1:
        raise ValueError('Choose a note explicitly using its note_id')
    return select(tx,pid,note_id)


def title_text(value):
    if not isinstance(value,str) or not value.strip() or len(value.strip())>200:
        raise ValueError('Use a note title of 1–200 characters')
    return value.strip()


def belongs(record, note):
    return record.get('project')==note['project'] and record.get('note_id',record.get('project'))==note['id']


def revision_number(value):
    if isinstance(value,bool) or not isinstance(value,int) or value<0:
        raise ValueError('A nonnegative integer revision is required')
    return value


def validate_text(value):
    if not isinstance(value,str) or len(value.encode('utf-8'))>MAX_TEXT:
        raise ValueError('Notes must be text of at most 500 KB')
    return value


def sections(text):
    found=[];fence=None
    for match in re.finditer(r'(?m)^.*(?:\n|$)',text):
        line=match[0].rstrip('\r\n')
        code=re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$',line)
        if code:
            marker=code[1][0]
            if fence is None:fence=(marker,len(code[1]))
            elif fence[0]==marker and len(code[1])>=fence[1] and not code[2].strip():fence=None
            continue
        if fence:continue
        heading=re.match(r'^(#{1,6})\s+(.+?)\s*#*\s*$',line)
        if heading:
            title=heading[2];occurrence=sum(s['title']==title for s in found)
            key=hashlib.sha256(title.encode()).hexdigest()[:12]+'-'+str(occurrence+1)
            found.append(dict(key=key,title=title,level=len(heading[1]),start=match.start(),heading=line))
    for i,section in enumerate(found):
        section['end']=next((other['start'] for other in found[i+1:] if other['level']<=section['level']),len(text))
    return found


def check_revision(note, expected):
    if note['revision']!=revision_number(expected):
        raise Conflict('The notes changed. Your draft is preserved; reload or compare versions before saving.')


def write(tx, ident, markdown, author, expected):
    note=current(tx,ident);check_revision(note,expected);validate_text(markdown)
    if note['markdown']==markdown:return note
    if note['revision']==0:
        tx.put('research_note_revisions',dict(note,note_id=ident,id=ident+':0'))
    value=dict(note,markdown=markdown,revision=note['revision']+1,updated=utc(),author=author)
    tx.put('research_notes',value)
    tx.put('research_note_revisions',dict(value,note_id=ident,id=ident+':'+str(value['revision'])))
    tx.event('research_notes_saved',project=note['project'],note_id=ident,revision=value['revision'],author=author)
    return value


def metadata(note):
    return dict(project=note['project'],note_id=note['id'],title=note.get('title','Research Notes'),revision=note['revision'],path=PATH if note['id']==note['project'] else note_path(note),updated=note['updated'],
                sections=[{k:s[k] for k in ('key','title','level')} for s in sections(note['markdown'])])


def proposal_conflict(note, proposal):
    """An older proposal is safe only when its exact target source survives."""
    if proposal.get('mode')=='comment':return 'This is feedback, not an edit to accept.'
    try:scope=resolve_section(note['markdown'],proposal.get('section',''))
    except ValueError:return 'The target section was removed or renamed. Ask for an updated suggestion.'
    source=note['markdown'][scope['start']:scope['end']] if scope else note['markdown']
    before=proposal.get('before')
    if not isinstance(before,str):
        return 'The original source is unavailable. Ask for an updated suggestion.'
    if source!=before:
        return ('The target section has changed' if scope else 'The document has changed')+'. Ask for an updated suggestion; your current notes are preserved.'
    return None


def view(tx, ident):
    note=current(tx,ident)
    history=[{k:r[k] for k in ('revision','updated','author')} for r in sorted((r for r in tx.all('research_note_revisions') if belongs(r,note)),key=lambda r:r['revision'],reverse=True)[:100]]
    proposals=sorted((r for r in tx.all('research_note_proposals') if belongs(r,note) and r['status']=='pending'),key=lambda r:r['created'],reverse=True)[:100]
    proposals=[dict(proposal,can_accept=(reason is None),accept_blocked_reason=reason or '')
               for proposal in proposals for reason in [proposal_conflict(note,proposal)]]
    grants=[dict(r,stale=r['base_revision']!=note['revision']) for r in tx.all('research_note_grants') if belongs(r,note) and r['status']=='active']
    return dict(note=note,archive_on_remove=bool(note['markdown'].strip()) or any(belongs(r,note) and r.get('markdown','').strip() for r in tx.all('research_note_revisions')),archived_notes=[metadata(n) for n in archived_notebooks(tx,note['project'])],notes=[metadata(n) for n in notebooks(tx,note['project'])],sections=metadata(note)['sections'],history=history,proposals=proposals,grants=grants)


def resolve_section(text, reference):
    if not reference or reference=='all':return None
    found=[s for s in sections(text) if s['key']==reference or s['title']==reference]
    if len(found)!=1:raise ValueError('Use an unambiguous section title or copied section key')
    return found[0]


def grant(tx, ident, expected, section='', author='human'):
    note=current(tx,ident);check_revision(note,expected)
    scope=resolve_section(note['markdown'],section)
    value=dict(id=uid(),project=note['project'],note_id=ident,base_revision=note['revision'],section=scope['key'] if scope else '',
               section_title=scope['title'] if scope else 'Whole document',status='active',created=utc(),author=author)
    tx.put('research_note_grants',value);tx.event('research_notes_permission_granted',project=note['project'],note_id=ident,grant=value['id'])
    return value


def apply_change(text, change):
    replacement=validate_text(change.get('replacement',''));scope=resolve_section(text,change.get('section',''))
    mode=change.get('mode','replace')
    if mode not in ('replace','append','comment'):raise ValueError('Invalid notes change mode')
    if mode=='comment':return text
    start,end=(scope['start'],scope['end']) if scope else (0,len(text))
    if scope:
        headings=sections(replacement)
        if mode=='replace':
            if not replacement.startswith(scope['heading']+'\n') and replacement!=scope['heading']:
                raise ValueError('A section replacement must preserve its heading')
            headings=headings[1:]
        if any(s['level']<=scope['level'] for s in headings):
            raise ValueError('Changes may not escape the authorized section hierarchy')
    result=text[:start]+(text[start:end].rstrip()+'\n\n'+replacement if mode=='append' else replacement).rstrip()+'\n\n'+text[end:]
    return validate_text(result)


def add_asset(root, tx, ident, data, name='Figure', note_id=None):
    select(tx,ident,note_id)
    if not isinstance(data,bytes) or len(data)>8_000_000:raise ValueError('Use PNG/JPEG figures smaller than 8 MB')
    from PIL import Image
    try:
        image=Image.open(io.BytesIO(data))
        if image.format not in ('PNG','JPEG') or image.width*image.height>20_000_000:raise ValueError('Unsupported figure format or dimensions')
        extension='png' if image.format=='PNG' else 'jpg';image.verify()
    except Exception as error:raise ValueError('Use a valid PNG or JPEG figure (at most 20 megapixels)') from error
    ident_asset=uid();relative='notes/research-notes-assets/'+ident_asset+'.'+extension
    path=Path(root)/'runtime/research-notes-assets'/ident/(ident_asset+'.'+extension)
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    asset=dict(id=ident_asset,project=ident,note_id=note_id or ident,name=str(name)[:200],path=str(path),relative=relative,mime='image/png' if extension=='png' else 'image/jpeg')
    tx.put('research_note_assets',asset)
    return {k:asset[k] for k in ('id','project','relative','name')}


def stage_figures(root,tx,session,change):
    replacement=change.get('replacement','')
    figures=change.get('figure_paths',[])
    if not isinstance(figures,list) or len(figures)>20 or any(not isinstance(p,str) for p in figures):raise ValueError('Use a list of at most 20 figure paths')
    for relative in figures:
        p=project(tx,session['project']);base=Path(root)/'projects'/p['folder']
        try:path=scoped(base,relative)
        except ValueError:
            if not session.get('workspace'):raise
            path=scoped(session['workspace'],relative)
        if path.stat().st_size>8_000_000:raise ValueError('Plot is too large')
        asset=add_asset(root,tx,session['project'],path.read_bytes(),path.name,note_id=change.get('note_id'))
        if relative in replacement:replacement=replacement.replace(relative,asset['relative'])
        else:replacement+='\n\n![Plot]('+asset['relative']+')'
    return dict(change,replacement=replacement)


def propose(root,tx,session,change):
    ident=target(tx,session['project'],change.get('note_id'));note=current(tx,ident);base=revision_number(change.get('base_revision'))
    original=note if base==note['revision'] else tx.get('research_note_revisions',ident+':'+str(base))
    if not original:raise ValueError('Unknown base notes revision')
    scope=resolve_section(original['markdown'],change.get('section',''))
    change=stage_figures(root,tx,session,change)
    mode=change.get('mode','replace');candidate=apply_change(original['markdown'],change)
    comment=change.get('comment','');validate_text(comment)
    if mode=='comment' and not comment.strip():raise ValueError('Feedback needs a comment')
    if mode!='comment' and candidate==original['markdown']:raise ValueError('The proposal has no changes')
    value=dict(id=uid(),project=note['project'],note_id=ident,base_revision=base,section=scope['key'] if scope else '',section_title=scope['title'] if scope else 'Whole document',
               mode=mode,replacement=change.get('replacement',''),before=original['markdown'][scope['start']:scope['end']] if scope else original['markdown'],comment=comment,summary=str(change.get('summary','Proposed change'))[:1000],
               status='pending',created=utc(),role=session['role'],session=session['id'])
    tx.put('research_note_proposals',value);tx.event('research_notes_proposed',project=note['project'],note_id=ident,proposal=value['id'])
    return value


def accept(tx, ident, proposal_id, expected, author='human', grant_id=None):
    note=current(tx,ident);check_revision(note,expected)
    proposal=tx.get('research_note_proposals',proposal_id)
    if not proposal or not belongs(proposal,note) or proposal['status']!='pending':raise ValueError('Proposal is unavailable')
    conflict=proposal_conflict(note,proposal)
    if conflict:raise Conflict(conflict)
    if grant_id:
        permission=tx.get('research_note_grants',grant_id)
        if not permission or not belongs(permission,note) or permission['status']!='active' or permission['base_revision']!=note['revision'] or (permission['section'] and permission['section']!=proposal['section']):
            raise ValueError('A current, matching one-time human permission is required')
        permission['status']='used';tx.put('research_note_grants',permission)
    markdown=apply_change(note['markdown'],proposal)
    saved=write(tx,ident,markdown,author,note['revision'])
    proposal.update(status='accepted',resolved=utc(),accepted_revision=saved['revision']);tx.put('research_note_proposals',proposal)
    return saved


def atomic_text(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',dir=path.parent,delete=False) as temp:
        temp.write(text);name=temp.name
    os.replace(name,path)


def export(root, database, ident):
    with database.tx() as tx:
        note=current(tx,ident);p=project(tx,ident);base=Path(root)/'projects'/p['folder']
        if note['revision']==0 and not tx.get('research_notes',ident):return
        atomic_text(base/note_path(note),f'<!-- Research Notes revision {note["revision"]}; human-owned. Agent changes require proposal/permission actions. -->\n\n'+note['markdown'])
        if ident==note['project']:
            atomic_text(base/PATH,f'<!-- Default Research Notes; note_id {ident}; revision {note["revision"]} -->\n\n'+note['markdown'])
        for asset in tx.all('research_note_assets'):
            if asset['project']==note['project']:
                target=base/asset['relative'];target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists():target.write_bytes(Path(asset['path']).read_bytes())


def human_action(root,ident,payload):
    if not isinstance(payload,dict):raise ValueError('Expected a JSON object')
    database=store(root);operation=payload.get('operation','save');removed_assets=[]
    with database.tx() as tx:
        pid=ident
        if not tx.get('projects',pid):raise ValueError('Unknown project')
        if operation in ('view_archive','unarchive'):
            archived=archived_note(tx,pid,payload.get('archived_note_id'))
            if operation=='view_archive':return dict(note=archived)
            archived={k:v for k,v in archived.items() if k not in ('archived','deleted')}
            tx.put('research_notes',archived)
            tx.event('research_note_unarchived',project=pid,note_id=archived['id'])
            ident=archived['id']
        else:ident=select(tx,pid,payload.get('note_id'))
        if operation=='create':
            ident=uid()
            tx.put('research_notes',dict(id=ident,project=pid,title=title_text(payload.get('title')),revision=0,markdown='',updated=utc(),author='human'))
        note=current(tx,ident)
        if operation=='unarchive':pass
        elif operation=='create':tx.event('research_note_created',project=pid,note_id=ident,title=note['title'])
        elif operation=='delete':
            check_revision(note,payload.get('revision'))
            if 'markdown' in payload:
                note=write(tx,ident,payload['markdown'],'human',note['revision'])
            has_content=bool(note['markdown'].strip()) or any(belongs(r,note) and r.get('markdown','').strip() for r in tx.all('research_note_revisions'))
            if has_content:
                tx.put('research_notes',dict(note,archived=utc()))
            else:
                # Keep only an empty identity tombstone, preventing stale clients from recreating it.
                for kind in ('research_note_revisions','research_note_proposals','research_note_grants'):
                    for record in tx.all(kind):
                        if belongs(record,note):tx.conn.execute('DELETE FROM records WHERE kind=? AND id=?',(kind,record['id']))
                tx.put('research_notes',dict(id=ident,project=pid,deleted=utc(),markdown='',revision=0,title=''))
                sources=tx.all('research_notes')+tx.all('research_note_revisions')
                for asset in tx.all('research_note_assets'):
                    if belongs(asset,note) and not any(asset['relative'] in r.get('markdown','') for r in sources):
                        tx.conn.execute('DELETE FROM records WHERE kind=? AND id=?',('research_note_assets',asset['id']))
                        removed_assets.append(asset)
            for record in tx.all('research_note_grants'):
                if belongs(record,note) and record['status']=='active':
                    tx.put('research_note_grants',dict(record,status='revoked',resolved=utc()))
            if not notebooks(tx,pid):
                replacement=uid()
                tx.put('research_notes',dict(id=replacement,project=pid,title='Research Notes',revision=0,markdown='',updated=utc(),author='human'))
            next_id=notebooks(tx,pid)[0]['id']
            tx.event('research_note_archived' if has_content else 'research_note_deleted',project=pid,note_id=ident)
        elif operation=='rename':
            check_revision(note,payload.get('revision'))
            tx.put('research_notes',dict(note,title=title_text(payload.get('title')),updated=utc()))
            tx.event('research_note_renamed',project=pid,note_id=ident,title=title_text(payload.get('title')))
        elif operation=='save':write(tx,ident,payload.get('markdown'), 'human',payload.get('revision'))
        elif operation=='restore':
            old=tx.get('research_note_revisions',ident+':'+str(revision_number(payload.get('restore_revision'))))
            if not old:raise ValueError('Revision is unavailable')
            write(tx,ident,old['markdown'],'human: restore',payload.get('revision'))
        elif operation=='grant':grant(tx,ident,payload.get('revision'),payload.get('section',''))
        elif operation=='revoke':
            permission=tx.get('research_note_grants',payload.get('grant_id',''))
            if not permission or not belongs(permission,note):raise ValueError('Permission is unavailable')
            permission['status']='revoked';tx.put('research_note_grants',permission)
        elif operation=='accept':accept(tx,ident,payload.get('proposal_id'),payload.get('revision'))
        elif operation=='reject':
            proposal=tx.get('research_note_proposals',payload.get('proposal_id',''))
            if not proposal or not belongs(proposal,note):raise ValueError('Proposal is unavailable')
            proposal.update(status='rejected',resolved=utc());tx.put('research_note_proposals',proposal)
        elif operation=='upload':
            try:data=base64.b64decode(payload.get('data',''),validate=True)
            except Exception as error:raise ValueError('Invalid image encoding') from error
            result=add_asset(root,tx,pid,data,payload.get('name','Figure'),note_id=ident)
            return dict(asset=result)
        else:raise ValueError('Unknown notes operation')
    if operation=='delete':
        with database.tx() as tx:base=Path(root)/'projects'/project(tx,pid)['folder']
        for asset in removed_assets:
            Path(asset['path']).unlink(missing_ok=True)
            (base/asset['relative']).unlink(missing_ok=True)
        (base/note_path(note)).unlink(missing_ok=True)
        if ident==pid:(base/PATH).unlink(missing_ok=True)
        ident=next_id
    export(root,database,ident)
    with database.tx() as tx:return view(tx,ident)
