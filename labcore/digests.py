"""Supervisor-published, versioned literature digests, separate from study reports."""
import datetime as dt
import json
from urllib.parse import urlsplit
import re
import time


def validate_sections(sections):
    if not isinstance(sections, list) or len(sections)>24:
        raise ValueError('Digest sections must be a list of at most 24 topics')
    for section in sections:
        if not isinstance(section,dict) or set(section)-{'topic','summary','papers'}:
            raise ValueError('Invalid digest topic section')
        if not isinstance(section.get('topic'),str) or not section['topic'].strip() or len(section['topic'])>200:
            raise ValueError('Each digest section needs a topic')
        if 'summary' in section and (not isinstance(section['summary'],str) or len(section['summary'])>2000):
            raise ValueError('Invalid digest topic summary')
        papers=section.get('papers')
        if not isinstance(papers,list) or len(papers)>100:
            raise ValueError('Each digest section needs a paper list')
        for paper in papers:
            if not isinstance(paper,dict) or set(paper)-{'title','url','summary','details','canonical_id'}:
                raise ValueError('Invalid digest paper entry')
            for key,limit in (('title',600),('url',2000),('summary',4000),('details',16000)):
                value=paper.get(key)
                if not isinstance(value,str) or not value.strip() or len(value)>limit:
                    raise ValueError('Each digest paper needs '+key)
            url=urlsplit(paper['url'])
            if url.scheme not in ('https','http') or not url.netloc or url.username or url.password:
                raise ValueError('Digest paper links must be public HTTP(S) URLs without credentials')
            if 'canonical_id' in paper and (not isinstance(paper['canonical_id'],str) or len(paper['canonical_id'])>300):
                raise ValueError('Invalid digest paper identity')
    if len(json.dumps(sections))>150000:
        raise ValueError('Digest topic sections are too large')


def markdown(record):
    text=record['text'].strip()
    for section in record.get('sections',[]):
        text+='\n\n## '+section['topic']+'\n'
        if section.get('summary'):text+='\n'+section['summary']+'\n'
        for paper in section['papers']:
            text+='\n### '+paper['title']+'\n\n'+paper['url']+'\n\n'+paper['summary']+'\n\n'+paper['details']+'\n'
    return text


def publish(tx, session, action):
    record = action.get('digest_record', {})
    fields = ('topic', 'title', 'period_start', 'period_end', 'summary', 'text')
    if any(not isinstance(record.get(k), str) or not record[k].strip() for k in fields):
        raise ValueError('A digest needs topic, title, period_start, period_end, summary and Markdown text')
    if 'sections' in record:validate_sections(record['sections'])
    topic = record['topic'].strip()
    key = action.get('key', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', key):
        raise ValueError('Use a stable lowercase topic slug as the digest key')
    start, end = (dt.date.fromisoformat(record[k]) for k in ('period_start', 'period_end'))
    if start > end or (end-start).days > 31:
        raise ValueError('Digest coverage must be an ordered date range of at most 31 days')
    if len(record['text']) > 100000 or len(record['summary']) > 2000 or len(topic) > 200 or len(record['title']) > 300:
        raise ValueError('Keep digest metadata and text compact')
    ident = f"{session['project']}:{key}:{end.isoformat()}"
    previous = tx.get('digests', ident, {})
    digest = dict(record, id=ident, project=session['project'], topic=topic, key=key,
                  version=previous.get('version', 0)+1, updated=time.time(),
                  created=previous.get('created', time.time()), session=session['id'])
    tx.put('digests', digest)
    tx.put('digest_versions', digest, key=f"{ident}:{digest['version']}")
    tx.event('digest_published', project=session['project'], digest=ident, version=digest['version'])
    return digest
