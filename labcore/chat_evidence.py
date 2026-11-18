"""Small read-only MCP evidence server. Every source is authorized by workspace."""
import base64
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

if __package__ in (None,''):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from labcore.dashboard import Dashboard
from labcore.store import Store
from labcore.files import paper_folder, scoped
from labcore import chat_figures


class Evidence:
    def __init__(self,root,pid,conversation_id=None):
        self.root=Path(root).resolve();self.pid=pid;self.conversation_id=conversation_id;self.store=Store(self.root/'runtime/lab.sqlite3');self.sources={}
        project=self.store.get('projects',pid)
        if not project:raise ValueError('Unknown workspace')
        data,paths=Dashboard(root).snapshot()
        def add(key,title,text='',url='#library',path=None):self.sources[key]=dict(id=key,title=title,text=text,url=url,path=path)
        from labcore import research_notes as notes
        with self.store.tx() as tx:documents=notes.notebooks(tx,pid)
        for note in documents:
            add('notes' if note['id']==pid else 'notes:'+note['id'],note['title'],note['markdown'],'#notes/'+note['id'])
        for kind in ('studies','digests','reports','research_stories'):
            for row in data[kind]:
                if row.get('project')==pid:add(kind+':'+row['id'],row.get('title') or row.get('summary','')[:100] or kind,json.dumps(row,ensure_ascii=False),'#story' if kind in ('studies','research_stories') else '#library')
        paper_ids={x['paper'] for x in data['paper_links'] if x['project']==pid}
        sessions=self.store.all('sessions')
        for summary in data['papers']:
            row=self.store.get('papers',summary['canonical_id'],summary)
            if row['canonical_id'] not in paper_ids:continue
            add('paper:'+row['canonical_id'],row.get('title','Paper'),json.dumps(row,ensure_ascii=False),row.get('url') or '#library')
            source_file=row.get('source_file')
            if row.get('task') and isinstance(source_file,str) and not Path(source_file).is_absolute():
                for session in reversed(sessions):
                    if session.get('task')!=row.get('task') or not session.get('workspace'):continue
                    workspace=Path(session['workspace']).resolve()
                    if not workspace.is_relative_to(self.root/'workspaces'):continue
                    try:source=scoped(workspace,source_file)
                    except ValueError:continue
                    if source.is_file() and source.suffix.lower() in ('.pdf','.md','.txt'):
                        add('papertext:'+row['canonical_id'],row.get('title','Paper')+' · registered full text',url=row.get('url') or '#library',path=source)
                        break
            folder=self.root/'library/papers'/paper_folder(row['canonical_id'])
            if folder.is_dir() and not folder.is_symlink():
                for f in folder.iterdir():
                    if f.is_file() and not f.is_symlink() and f.suffix.lower() in ('.pdf','.md','.txt'):
                        add('paperfile:'+row['canonical_id']+':'+f.name,f.name,path=f)
        for f in data['files']:
            if f['project']==pid:add('file:'+f['id'],f['path'],url='/api/file?id='+f['id'],path=paths.get(f['id']))
        # Numerical data and plotting code are not all in the dashboard preview index.
        base=(self.root/'projects'/project['folder']).resolve()
        if base.is_relative_to(self.root/'projects') and base!=self.root/'projects':
            count=0
            for section in ('code','experiments','reports','runs'):
                for path in (base/section).rglob('*'):
                    rel=path.relative_to(base)
                    if any(part.startswith('.') or part in ('control','tmp','node_modules','__pycache__') for part in rel.parts):continue
                    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(base) or path.suffix.lower() not in ('.py','.csv','.tsv','.json','.jsonl','.npy','.npz','.svg'):continue
                    if path.stat().st_size>20_000_000:continue
                    import hashlib
                    key='data:'+hashlib.sha256(str(rel).encode()).hexdigest()[:24]
                    add(key,str(rel),path=path);count+=1
                    if count>=4000:break
                if count>=4000:break
        for asset in self.store.all('research_chat_assets'):
            if asset['project']==pid:
                add('chatasset:'+asset['id'],asset.get('caption') or asset['name'],url=chat_figures.public(asset)['url'],path=Path(asset['path']))
        for a in self.store.all('research_note_assets'):
            if a['project']==pid:add('figure:'+a['id'],a.get('name','Notes figure'),url='/api/research-notes/asset?project='+pid+'&asset='+a['id'],path=Path(a['path']))

    def call(self,name,args):
        if not isinstance(args,dict):raise ValueError('Expected arguments object')
        if name=='show_figure':return chat_figures.show_figure(self,args)
        if name=='run_plot':return chat_figures.run_plot(self,args)
        if name=='search_evidence':
            query=args.get('query','')
            if not isinstance(query,str) or len(query)>500:raise ValueError('Use a short search query')
            words=re.findall(r'\w+',query.lower());rank=[]
            for s in self.sources.values():
                hay=(s['title']+' '+s['text']).lower();score=sum(w in hay for w in words)
                if not words or score:rank.append((score,s))
            rank.sort(key=lambda pair:pair[0],reverse=True)
            return [dict(type='text',text=json.dumps(dict(matches=[{k:s[k] for k in ('id','title','url')}|dict(excerpt=s['text'][:600]) for _,s in rank[:30]],total=len(rank)),ensure_ascii=False))]
        if name=='propose_notes_edit':
            from labcore import research_notes as notes
            if not self.conversation_id:raise ValueError('Notes proposals require an active research conversation')
            with self.store.tx() as tx:
                chat=tx.get('research_chats',self.conversation_id)
                if not chat or chat['project']!=self.pid or chat.get('status')!='running' or chat.get('cancel_requested'):raise ValueError('This conversation is not active in this workspace')
                ident=notes.target(tx,self.pid,args.get('note_id'))
                current=notes.current(tx,ident)
                if args.get('base_revision')!=current['revision']:raise ValueError('Notes changed. Read the latest notes revision before proposing an edit.')
                change={k:args[k] for k in ('note_id','base_revision','section','mode','replacement','comment','summary') if k in args}
                change['note_id']=ident
                scope=notes.resolve_section(current['markdown'],change.get('section',''))
                change['section']=scope['key'] if scope else ''
                for old in tx.all('research_note_proposals'):
                    if old.get('project')==self.pid and old.get('note_id',self.pid)==ident and old.get('session')==self.conversation_id and old.get('status')=='pending' and all(old.get(k,'')==change.get(k,'') for k in ('base_revision','section','mode','replacement','comment')):
                        return [dict(type='text',text=json.dumps(dict(proposal=old['id'],status='pending',url='#notes/'+ident,message='This proposal already awaits human review. The original notes are unchanged.')))]
                proposal=notes.propose(self.root,tx,dict(id=self.conversation_id,project=self.pid,role='research chat'),change)
            return [dict(type='text',text=json.dumps(dict(proposal=proposal['id'],status='pending',url='#notes/'+ident,message='Suggested change submitted. The human can review and accept or reject it in Research Notes. The original notes are unchanged.')))]
        source=self.sources.get(args.get('source'))
        if not source:raise ValueError('Source is not available in this workspace. Search for an authorized source ID first.')
        path=source['path'];page=args.get('page',1);start=args.get('start',0)
        if isinstance(page,bool) or not isinstance(page,int) or page<1:raise ValueError('Page must be a positive integer')
        if isinstance(start,bool) or not isinstance(start,int) or start<0:raise ValueError('Start must be nonnegative')
        if path and path.stat().st_size>20_000_000:raise ValueError('This file is too large for chat reading; use a smaller report or extracted section')
        if name=='view_figure':
            if not path:raise ValueError('Choose an image file or PDF page')
            if path.suffix.lower()=='.pdf':
                from pypdf import PdfReader
                if page>len(PdfReader(path).pages):raise ValueError('Page unavailable')
                dependency=Path(__file__).resolve().parents[1]/'runtime/research-chat-deps'
                if str(dependency) not in sys.path:sys.path.insert(0,str(dependency))
                import pypdfium2 as pdfium
                document=pdfium.PdfDocument(path)
                try:
                    pdfpage=document[page-1]
                    try:
                        bitmap=pdfpage.render(scale=min(2,1600/max(pdfpage.get_size())))
                        try:
                            out=io.BytesIO();bitmap.to_pil().save(out,format='PNG');data=out.getvalue()
                        finally:bitmap.close()
                    finally:pdfpage.close()
                finally:document.close()
            else:
                from PIL import Image
                image=Image.open(path)
                if image.width*image.height>20000000:raise ValueError('Image is too large')
                image.thumbnail((1600,1600));out=io.BytesIO();image.convert('RGB').save(out,format='PNG');data=out.getvalue()
            return [dict(type='text',text=source['title']+' · '+source['url']+(('#page='+str(page)) if path.suffix.lower()=='.pdf' else '')),dict(type='image',data=base64.b64encode(data).decode(),mimeType='image/png')]
        if name!='read_evidence':raise ValueError('Unknown evidence tool')
        text=source['text'];extra={}
        if source['id']=='notes' or source['id'].startswith('notes:'):
            from labcore import research_notes as notes
            with self.store.tx() as tx:
                ident=notes.select(tx,self.pid,self.pid if source['id']=='notes' else source['id'].split(':',1)[1])
                note=notes.current(tx,ident);text=note['markdown'];extra=dict(note_id=ident,revision=note['revision'],sections=[{k:s[k] for k in ('key','title','level')} for s in notes.sections(text)])
        if path:
            if path.suffix.lower()=='.pdf':
                from pypdf import PdfReader
                pdf=PdfReader(path)
                if page>len(pdf.pages):raise ValueError('Page unavailable')
                text=pdf.pages[page-1].extract_text() or '';extra=dict(page=page,pages=len(pdf.pages))
            elif path.suffix.lower() in ('.png','.jpg','.jpeg','.webp'):raise ValueError('Use view_figure for this image')
            elif path.suffix.lower() in ('.npy','.npz'):text='NumPy binary data, '+str(path.stat().st_size)+' bytes. Use run_plot with this source ID to inspect arrays and generate figures.'
            else:text=path.read_text(errors='replace')
        return [dict(type='text',text=json.dumps(dict(id=source['id'],title=source['title'],url=source['url'],text=text[start:start+30000],next_start=start+30000 if start+30000<len(text) else None,**extra),ensure_ascii=False))]

TOOLS=[dict(name='search_evidence',description='Search the current workspace library/digests, studies, reports, plots and notes. Empty query lists available evidence; returns source IDs and citation links. Search filenames for PDF/plots.',inputSchema=dict(type='object',properties=dict(query=dict(type='string')),required=['query'])),dict(name='read_evidence',description='Read an authorized evidence source. For PDF use page (1-based); for long text use start offset. Includes exact source citation.',inputSchema=dict(type='object',properties=dict(source=dict(type='string'),page=dict(type='integer',minimum=1),start=dict(type='integer',minimum=0)),required=['source'])),dict(name='view_figure',description='See an authorized plot/image or render one PDF page. Use for actual figures, scanned pages or equation layouts.',inputSchema=dict(type='object',properties=dict(source=dict(type='string'),page=dict(type='integer',minimum=1)),required=['source']))]

TOOLS.append(dict(name='propose_notes_edit',description='When the human asks for suggested Research Notes edits, submit a proposal for human review. NEVER changes the original. First read source notes for current revision and sections. For replace of a section preserve its heading; use Markdown and LaTeX, preserve alignment containers. The human accepts/rejects in Research Notes.',inputSchema=dict(type='object',properties=dict(note_id=dict(type='string',description='Permanent note ID returned by read_evidence'),base_revision=dict(type='integer',minimum=0),section=dict(type='string',description='Existing section key/title, or empty for whole document'),mode=dict(type='string',enum=['replace','append','comment']),replacement=dict(type='string'),comment=dict(type='string'),summary=dict(type='string')),required=['base_revision','mode','summary'],additionalProperties=False)))

TOOLS.extend([
 dict(name='show_figure',description='Attach an existing plot/image or a rendered PDF page directly to the human conversation. Use when asked to show a figure; returns an inline attachment and image. Originals unchanged.',inputSchema=dict(type='object',properties=dict(source=dict(type='string'),page=dict(type='integer',minimum=1),caption=dict(type='string')),required=['source'],additionalProperties=False)),
 dict(name='run_plot',description='Run a small CPU-only Python plotting/data-formatting operation on copies of authorized workspace evidence. Use only for human-requested figure/table changes or regeneration. Globals INPUTS maps supplied source IDs to staged file paths; OUTPUT_DIR is the only output directory. Use matplotlib Agg, numpy/pandas/Pillow as available; write PNG for inline display and optional PDF/CSV. No network, shell, subprocesses, GPU or original-file writes. Limits: 60 seconds wall/30 CPU seconds, 2 GB RAM, 16 inputs <=20 MB, 12 outputs <=64 MB total. Return reproducible copies only; never invent data or treat edits as verified experiment results.',inputSchema=dict(type='object',properties=dict(sources=dict(type='array',items=dict(type='string'),maxItems=16),code=dict(type='string'),caption=dict(type='string')),required=['sources','code','caption'],additionalProperties=False))
])

def main():
    evidence=None
    for line in sys.stdin:
        try:
            req=json.loads(line);ident=req.get('id');method=req.get('method')
            if ident is None:continue
            if method=='initialize':result=dict(protocolVersion=req.get('params',{}).get('protocolVersion','2024-11-05'),capabilities=dict(tools={}),serverInfo=dict(name='workspace-research',version='1.0'))
            elif method=='tools/list':result=dict(tools=TOOLS)
            elif method=='tools/call':
                if evidence is None:evidence=Evidence(sys.argv[1],sys.argv[2],sys.argv[3] if len(sys.argv)>3 else None)
                params=req['params']
                try:result=dict(content=evidence.call(params['name'],params.get('arguments',{})),isError=False)
                except Exception as error:result=dict(content=[dict(type='text',text=str(error)[:1000])],isError=True)
            elif method=='ping':result={}
            else:
                print(json.dumps(dict(jsonrpc='2.0',id=ident,error=dict(code=-32601,message='Unsupported method'))),flush=True);continue
            print(json.dumps(dict(jsonrpc='2.0',id=ident,result=result)),flush=True)
        except Exception as error:
            print(json.dumps(dict(jsonrpc='2.0',id=req.get('id') if isinstance(req,dict) else None,error=dict(code=-32603,message=str(error)[:500]))),flush=True)
if __name__=='__main__':main()
