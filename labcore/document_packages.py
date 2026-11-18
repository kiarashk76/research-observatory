"""Surface unified study reports and legacy pairs; acceptance is separate."""
import re
import time
from pathlib import Path


def register(tx,root,session,manifest):
    project=tx.get('projects',session['project'])
    base=(Path(root)/'projects'/project['folder']).resolve()
    grouped={}
    for evidence in manifest:
        path=Path(evidence.get('path','')).resolve()
        if not path.is_relative_to(base) or not path.is_file():continue
        match=re.fullmatch(r'(.+)_(paper_section|detailed_report)\.pdf',path.name,re.I)
        if match:prefix,kind=match.groups()
        else:
            match=re.fullmatch(r'(.+?)(?:_study)?_report\.pdf',path.name,re.I)
            if not match:continue
            prefix=match.group(1);kind='report'
        key=re.sub(r'[^a-z0-9]+','-',prefix.lower()).strip('-')
        if not key:continue
        grouped.setdefault(key,{'label':prefix.replace('_',' ')})[kind.lower()]=path.relative_to(base).as_posix()
    for key,files in grouped.items():
        if 'report' not in files and not all(k in files for k in ('paper_section','detailed_report')):continue
        record=dict(id=project['id']+':'+key,project=project['id'],study_key=key,label=files['label'],
            task=session.get('task'),
            session=session['id'],submitted_at=time.time())
        if 'report' in files:record['report_pdf']=files['report']
        else:record.update(paper_pdf=files['paper_section'],detailed_pdf=files['detailed_report'])
        tx.put('document_packages',record)
