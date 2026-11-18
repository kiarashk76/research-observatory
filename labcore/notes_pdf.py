"""Bounded, local-only, cached PDF rendering; no model calls or GPU work."""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading

_LOCK=threading.BoundedSemaphore(1)


def export_pdf(root,note,figures,title):
    root=Path(root);source=root/'frontend/research-notes/pdf.mjs';assets=root/'labcore/dashboard/notes-vendor'
    node=root/'runtime/tools/node/bin/node'
    images={a['relative']:'data:'+a['mime']+';base64,'+base64.b64encode(Path(a['path']).read_bytes()).decode() for a in figures if a['relative'] in note['markdown']}
    stamp=hashlib.sha256((note['markdown']+json.dumps(images,sort_keys=True)+str(note['revision'])+title+str(source.stat().st_mtime_ns)+str((source.parent/'render.mjs').stat().st_mtime_ns)+str((assets/'notes-editor.css').stat().st_mtime_ns)).encode()).hexdigest()[:20]
    folder=root/'runtime/research-notes-pdfs';folder.mkdir(exist_ok=True)
    target=folder/(note['project']+'-'+stamp+'.pdf')
    if target.exists():return target.read_bytes()
    if not _LOCK.acquire(timeout=3):raise RuntimeError('Another PDF is being prepared')
    try:
        if target.exists():return target.read_bytes()
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            output=Path(temp)/'notes.pdf'
            data=dict(markdown=note['markdown'],revision=note['revision'],project=note['project'],title=title,images=images,assets=str(assets),output=str(output))
            env=dict(os.environ,PLAYWRIGHT_BROWSERS_PATH=str(root/'runtime/tools/playwright'))
            proc=subprocess.Popen([str(node),str(source)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,start_new_session=True)
            try:stdout,stderr=proc.communicate(json.dumps(data).encode(),timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL);proc.communicate();raise RuntimeError('PDF rendering timed out')
            if proc.returncode or not output.exists():raise RuntimeError('PDF rendering failed: '+stderr.decode(errors='replace')[-400:])
            result=output.read_bytes()
            if not result.startswith(b'%PDF-'):raise RuntimeError('Invalid PDF output')
            output.replace(target)
            return result
    finally:_LOCK.release()
