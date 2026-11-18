"""Validated chat figure attachments and reproducible, sandboxed plotting copies."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from .store import uid,utc

ALLOWED={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.pdf':'application/pdf','.csv':'text/csv','.tsv':'text/tab-separated-values','.json':'application/json','.txt':'text/plain','.md':'text/plain','.py':'text/plain'}


def active(evidence):
    chat=evidence.store.get('research_chats',evidence.conversation_id or '')
    if not chat or chat.get('project')!=evidence.pid or chat.get('status')!='running' or chat.get('cancel_requested') or chat.get('deleted'):raise ValueError('Figure tools require an active conversation in this workspace')
    return chat


def public(asset):
    result={k:asset[k] for k in ('id','project','conversation','name','caption','mime','kind','source_url','inputs') if k in asset}
    result['source_id']='chatasset:'+asset['id']
    result['url']='/api/research-chat-asset?project='+asset['project']+'&asset='+asset['id']
    return result


def add_asset(evidence,data,name,caption='',kind='retrieved',source_url='',inputs=None):
    chat=active(evidence);suffix=Path(name).suffix.lower()
    if suffix not in ALLOWED or len(data)>20_000_000:raise ValueError('Use a supported artifact of at most 20 MB')
    if suffix in ('.png','.jpg','.jpeg'):
        from PIL import Image
        image=Image.open(io.BytesIO(data))
        if image.format not in ('PNG','JPEG') or image.width*image.height>20000000:raise ValueError('Use a PNG/JPEG of at most 20 megapixels')
        image.verify()
    elif suffix=='.pdf':
        from pypdf import PdfReader
        if len(PdfReader(io.BytesIO(data)).pages)>200:raise ValueError('Chat PDFs are limited to 200 pages')
    ident=uid();folder=evidence.root/'runtime/research-chat-assets'/evidence.pid;folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=folder/(ident+suffix);path.write_bytes(data)
    asset=dict(id=ident,project=evidence.pid,conversation=chat['id'],turn=chat.get('current_message'),name=Path(name).name[:160],caption=str(caption)[:1000],mime=ALLOWED[suffix],kind=kind,source_url=source_url,inputs=inputs or [],path=str(path),created=utc())
    with evidence.store.tx() as tx:
        latest=tx.get('research_chats',chat['id'])
        if latest.get('cancel_requested') or latest.get('status')!='running':path.unlink(missing_ok=True);raise ValueError('Conversation stopped before the figure was ready')
        tx.put('research_chat_assets',asset);latest['live_assets']=latest.get('live_assets',[])+[public(asset)];tx.put('research_chats',latest)
    info=public(asset);evidence.sources[info['source_id']]=dict(id=info['source_id'],title=asset['caption'] or asset['name'],text='',url=info['url'],path=path)
    return info


def show_figure(evidence,args):
    active(evidence);source=evidence.sources.get(args.get('source'))
    if not source:raise ValueError('Search for a figure source in this workspace first')
    content=evidence.call('view_figure',args)
    data=base64.b64decode(content[1]['data'])
    figure=add_asset(evidence,data,'figure.png',args.get('caption') or source['title'],'retrieved',source['url'],[dict(id=source['id'],title=source['title'])])
    return [dict(type='text',text=json.dumps(dict(attachment=figure,message='This figure is attached inline to the conversation. Explain it without duplicating the attachment.'))),content[1]]


def run_plot(evidence,args):
    chat=active(evidence);code=args.get('code');ids=args.get('sources',[])
    if not isinstance(code,str) or not code.strip() or len(code.encode())>50000:raise ValueError('Provide a Python plotting script smaller than 50 KB')
    if not isinstance(ids,list) or len(ids)>16 or any(not isinstance(i,str) for i in ids):raise ValueError('Choose at most 16 evidence source IDs')
    folder=evidence.root/'runtime/research-chat-plots'/evidence.pid;folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(dir=folder,prefix='plot-') as temporary:
        work=Path(temporary)
        for sub in ('input','output','tmp'):(work/sub).mkdir()
        inputs={};provenance=[]
        for i,ident in enumerate(dict.fromkeys(ids)):
            source=evidence.sources.get(ident)
            if not source:raise ValueError('Input source is outside this workspace')
            path=source.get('path')
            if path:
                if path.stat().st_size>20_000_000:raise ValueError('Plot inputs must be smaller than 20 MB each')
                data=path.read_bytes();name=path.name
            else:data=source['text'].encode();name='evidence.txt'
            target=work/'input'/(str(i)+'-'+name);target.write_bytes(data);inputs[ident]=str(target)
            provenance.append(dict(id=ident,title=source['title'],sha256=hashlib.sha256(data).hexdigest()))
        (work/'plot.py').write_text(code);manifest=work/'manifest.json';manifest.write_text(json.dumps(dict(work=str(work),inputs=inputs)))
        env=dict(PATH='/usr/bin:/bin',HOME=str(work/'tmp'),TMPDIR=str(work/'tmp'),MPLCONFIGDIR=str(work/'tmp'),MPLBACKEND='Agg',CUDA_VISIBLE_DEVICES='',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',NUMEXPR_NUM_THREADS='1',PYTHONNOUSERSITE='1')
        with (work/'stdout.txt').open('wb') as stdout,(work/'stderr.txt').open('wb') as stderr:
            proc=subprocess.Popen([sys.executable,str(Path(__file__).with_name('chat_plot_worker.py')),str(manifest)],env=env,cwd=work,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr)
            started=time.monotonic()
            try:
                while proc.poll() is None:
                    if time.monotonic()-started>60:raise ValueError('The small plotting operation exceeded 60 seconds. Use an engineering assignment for longer computation.')
                    written=0;count=0
                    for directory in (work/'output',work/'tmp'):
                        for item in directory.rglob('*'):
                            if item.is_symlink():raise ValueError('Plot outputs may not contain symlinks')
                            if item.is_file():written+=item.stat().st_size;count+=1
                            if written>64_000_000 or count>128:raise ValueError('Plot output exceeded its disk allowance')
                    time.sleep(.1)
            finally:
                if proc.poll() is None:proc.kill()
                proc.wait()

        log=(work/'stdout.txt').read_text(errors='replace')[-4000:]
        if proc.returncode:raise ValueError('Plot script failed: '+(work/'stderr.txt').read_text(errors='replace')[-3500:])
        active(evidence)
        outputs=[];total=0
        for output in sorted((work/'output').rglob('*')):
            if output.is_symlink():raise ValueError('Plot outputs may not contain symlinks')
            if not output.is_file() or output.suffix.lower() not in ALLOWED:continue
            total+=output.stat().st_size
            if len(outputs)>=12 or total>64_000_000:raise ValueError('Limit plot output to 12 files and 64 MB')
            outputs.append(output)
        if not outputs:raise ValueError('No supported output was saved. Write figure.png or figure.pdf under OUTPUT_DIR.')
        assets=[add_asset(evidence,out.read_bytes(),out.name,args.get('caption','Revised plotting output'),'revised','',provenance) for out in outputs]
        script=add_asset(evidence,code.encode(),'plot.py','Python used to generate these copies','script','',provenance)
        result=dict(attachments=assets,script=script,stdout=log,message='Outputs are attached to the chat. Original evidence is unchanged. Cite the inputs and explain your changes. Do not invent results or claim these copies update the experiment records.')
        return [dict(type='text',text=json.dumps(result))]
