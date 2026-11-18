"""Project-scoped study records and deterministic publication readiness checks.

Mechanical checks establish artifact availability, not scientific correctness.
Only the supervisor updates the registry; actual source artifacts remain immutable.
"""
import json
from pathlib import Path
import re
import time

from .files import digest, scoped
from .store import uid, utc

FIELDS = {'title','question','status_note','execution','verification','task_ids','verification_task','technical_record',
          'source_record','report_pdf','paper_pdf','detailed_pdf','story_draft','figures','consistency_record','qa_record','known_gaps','review_notes'}
PATH_FIELDS = ('technical_record','source_record','report_pdf','paper_pdf','detailed_pdf','story_draft','consistency_record','qa_record')
TECHNICAL_SECTIONS = ('question','data','methods','training','evaluation','implementation','runs','results',
                      'limitations','reproduction','missing_information','source_paths')


def artifact(base, relative):
    if not isinstance(relative,str) or not relative or Path(relative).is_absolute():
        raise ValueError('Study artifacts must use project-relative paths')
    path=scoped(base,relative,exists=False)
    if path.relative_to(base.resolve()).parts[0] not in {'reports','experiments','code','notes','runs'}:
        raise ValueError('Study artifacts must be project research files')
    return path


def inspect(base, record, *, table=False):
    """Bounded checks; return missing-work diagnostics rather than reject a report."""
    files={};issues=[];technical_gaps=[]
    pdfs=('report_pdf',) if record.get('report_pdf') and not table else ('paper_pdf','detailed_pdf')
    source='technical_record' if record.get('technical_record') else 'source_record'
    references={name:record.get(name) for name in (source,*pdfs,*(() if table else ('story_draft',)),'consistency_record','qa_record')}
    if not table:references.update({'figure:'+str(i):path for i,path in enumerate(record.get('figures',[]))})
    for name,relative in references.items():
        if not relative:
            issues.append(name+': not supplied');continue
        path=artifact(base,relative)
        if not path.is_file() or not 0<path.stat().st_size<=20*1024*1024:
            issues.append(name+': file missing, empty or above 20 MiB');continue
        stat=path.stat();files[name]=dict(path=relative,sha256=digest(path),size=stat.st_size,mtime_ns=stat.st_mtime_ns)
        if name in ('report_pdf','paper_pdf','detailed_pdf'):
            if path.suffix.lower()!='.pdf':
                issues.append(name+': must be a PDF');continue
            try:
                from pypdf import PdfReader
                reader=PdfReader(path,strict=False)
                if reader.is_encrypted:raise ValueError('encrypted')
                pages=len(reader.pages)
                if not 0<pages<=500:raise ValueError('invalid page count')
                readable=any(len((page.extract_text() or '').strip())>=40 for page in reader.pages[:min(5,pages)])
                if not readable:raise ValueError('no searchable text in opening pages')
                files[name]['pages']=pages
                if name=='report_pdf' and not any(re.search(r'^\s*Append(?:ix|ices)\b',page.extract_text() or '',re.I|re.M) for page in reader.pages):
                    issues.append('report_pdf: no appendix heading found; the report must include reproduction details')
            except Exception:
                issues.append(name+': PDF could not be parsed or searchable text is missing')
        elif name.startswith('figure:'):
            try:
                from PIL import Image
                if path.suffix.lower() not in {'.png','.jpg','.jpeg','.webp'}:raise ValueError('unsupported image')
                with Image.open(path) as image:image.verify()
            except Exception:issues.append(name+': figure is not a readable dashboard image')
        elif name=='source_record':
            if path.suffix.lower() not in {'.md','.txt','.json'}:
                issues.append('source_record: use a readable implementation/evidence record')
            else:
                try:
                    if len(path.read_text().strip())<40:issues.append('source_record: evidence record is empty or too short')
                except (OSError,UnicodeError):issues.append('source_record: evidence record is not readable text')
        elif name=='technical_record':
            try:
                def invalid(value):raise ValueError('nonfinite number')
                data=json.loads(path.read_text(),parse_constant=invalid)
                if not isinstance(data,dict):raise ValueError('not an object')
                missing=[key for key in TECHNICAL_SECTIONS if key not in data or data[key] is None]
                missing += [key for key in ('data','methods','training','evaluation','implementation','reproduction')
                            if key in data and not data[key]]
                if missing:issues.append('technical_record: missing sections '+', '.join(missing))
                if record.get('execution') == 'completed' and (not data.get('runs') or not data.get('results')):
                    issues.append('technical_record: completed study needs a run inventory and results')
                if re.search(r'\b(?:TODO|TBD|TO_BE_COMPLETED)\b',json.dumps(data)):
                    issues.append('technical_record: unfilled template placeholders remain')
                if not isinstance(data.get('missing_information'),list):issues.append('technical_record: missing_information must be a list')
                else:technical_gaps=data['missing_information']
                sources=data.get('source_paths')
                if not isinstance(sources,list) or not sources:issues.append('technical_record: source_paths must list actual evidence files')
                else:
                    for source in sources[:100]:
                        if not artifact(base,source).is_file():issues.append('technical_record: source file unavailable: '+str(source))
            except (OSError,ValueError,TypeError):issues.append('technical_record: must be a valid source-backed JSON record')
    if not table and not record.get('figures'):issues.append('figures: no key figure supplied')
    if (table or not record.get('report_pdf')) and files.get('paper_pdf',{}).get('sha256') and files.get('paper_pdf',{}).get('sha256') == files.get('detailed_pdf',{}).get('sha256'):
        issues.append('Paper section and detailed report must be two different documents')
    return dict(files=files,issues=issues,technical_gaps=technical_gaps,checked_at=time.time())


def update(engine, tx, session, action):
    if session['role']!='supervisor':raise ValueError('Only the supervisor publishes study records')
    slug=action.get('study_id','')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}',slug):raise ValueError('Study ID must be a stable lowercase slug')
    project=tx.get('projects',session['project']);base=(engine.root/'projects'/project['folder']).resolve()
    if not base.is_relative_to(engine.root/'projects') or base==engine.root/'projects':raise ValueError('Invalid project folder')
    key=project['id']+':'+slug;old=tx.get('studies',key,{})
    patch=action.get('study_record',{})
    if not isinstance(patch,dict) or set(patch)-FIELDS:raise ValueError('Unknown study record fields')
    record={name:old[name] for name in FIELDS if name in old};record.update(patch)
    if not record.get('title') or not record.get('question'):raise ValueError('Study needs a title and scientific question')
    record.setdefault('execution','planned');record.setdefault('verification','unverified')
    record.setdefault('task_ids',[]);record.setdefault('figures',[]);record.setdefault('known_gaps',[])
    for name in ('task_ids','figures','known_gaps'):
        if not isinstance(record[name],list) or len(record[name])>100 or any(not isinstance(x,str) for x in record[name]):
            raise ValueError(name+' must be a bounded list of strings')
    for name in FIELDS-{'task_ids','figures','known_gaps'}:
        if name in record and not isinstance(record[name],str):raise ValueError(name+' must be text')
    if record['execution'] not in ('planned','running','completed','failed') or record['verification'] not in ('unverified','under_review','verified','issues_found'):
        raise ValueError('Invalid study execution or verification state')
    for task_id in record['task_ids']:
        task=tx.get('tasks',task_id)
        if not task or task['project']!=project['id']:raise ValueError('Study task is outside this project')
    verifier=record.get('verification_task')
    if verifier:
        task=tx.get('tasks',verifier)
        if not task or task['project']!=project['id'] or task['role']!='verifier':raise ValueError('Verification task must be this project’s verifier assignment')
    if record['verification']=='verified':
        task=tx.get('tasks',verifier or '',{})
        if task.get('status')!='accepted' or task.get('verdict')!='pass':raise ValueError('Verified study needs an accepted passing verifier report')
    for name in PATH_FIELDS:
        if record.get(name):artifact(base,record[name])
    from .research_story import table_mode,sync_table
    table=table_mode(engine)
    checks=inspect(base,record,table=table)
    requested=bool(action.get('publish'))
    if requested:
        story=tx.get('project_notes',project['id']+':notes/research-story.md',{}).get('text','')
        linked=set(re.findall(r'!?\[[^\]]*\]\(([^\s)]+)\)',story))
        for name in (() if table else ('report_pdf',) if record.get('report_pdf') else ('paper_pdf','detailed_pdf')):
            if record.get(name) not in linked:checks['issues'].append(name+': not linked from the current Research story')
        images=set(re.findall(r'!\[[^\]]*\]\(([^\s)]+)\)',story))
        for path in ([] if table else record['figures']):
            if path not in images:checks['issues'].append('Key figure is not embedded in the current Research story: '+path)
        if not record.get('review_notes','').strip():checks['issues'].append('Supervisor document review has not been recorded')
    documentation='incomplete' if checks['issues'] else 'ready_for_review'
    if requested and not checks['issues']:
        documentation='published_with_gaps' if record['known_gaps'] or checks['technical_gaps'] else 'complete'
    record.update(id=key,study_id=slug,project=project['id'],documentation=documentation,checks=checks,
        revision=old.get('revision',0)+1,updated=time.time(),published_at=utc() if requested and not checks['issues'] else None)
    tx.put('studies',record)
    if table:sync_table(tx,project['id'])
    tx.put('study_versions',dict(record),key=uid())
    tx.event('study_updated',project=project['id'],study=slug,documentation=documentation)
    return record


def current_view(root, project, record):
    """Cheap live availability check for immutable, already-inspected artifacts."""
    value=dict(record);checks=dict(value.get('checks',{}));issues=list(checks.get('issues',[]))
    base=(Path(root)/'projects'/project['folder']).resolve()
    if not base.is_relative_to(Path(root).resolve()/'projects') or base==Path(root).resolve()/'projects':
        value.update(documentation='incomplete',checks={'issues':['Invalid project folder'],'files':{}})
        return value
    for name,info in checks.get('files',{}).items():
        try:
            path=artifact(base,info['path']);stat=path.stat()
            if not path.is_file() or stat.st_size!=info['size'] or stat.st_mtime_ns!=info['mtime_ns']:
                issues.append(name+': file changed since publication checks')
        except (OSError,ValueError,KeyError):issues.append(name+': file no longer available')
    checks['issues']=list(dict.fromkeys(issues));value['checks']=checks
    if issues:value['documentation']='incomplete'
    return value
