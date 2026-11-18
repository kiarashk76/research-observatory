"""Optional installed-CLI check against a local fake API; no paid model calls."""
import json, os, shutil, subprocess, tempfile, threading, uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
requests=[]
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0')))); requests.append(data)
        message=dict(id='msg_test',type='message',role='assistant',model='claude-haiku-4-5',content=[dict(type='text',text='ARCHIVE_REPLY_MARKER')],stop_reason='end_turn',stop_sequence=None,usage=dict(input_tokens=1,output_tokens=1))
        self.send_response(200)
        if data.get('stream'):
            self.send_header('Content-Type','text/event-stream');self.end_headers()
            events=[('message_start',dict(type='message_start',message=dict(message,content=[],stop_reason=None))),('content_block_start',dict(type='content_block_start',index=0,content_block=dict(type='text',text=''))),('content_block_delta',dict(type='content_block_delta',index=0,delta=dict(type='text_delta',text='ARCHIVE_REPLY_MARKER'))),('content_block_stop',dict(type='content_block_stop',index=0)),('message_delta',dict(type='message_delta',delta=dict(stop_reason='end_turn',stop_sequence=None),usage=dict(output_tokens=1))),('message_stop',dict(type='message_stop'))]
            for event,payload in events:self.wfile.write(('event: '+event+'\ndata: '+json.dumps(payload)+'\n\n').encode())
        else:
            self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(message).encode())
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp); work=root/'work';work.mkdir()
        env=dict(os.environ,ANTHROPIC_API_KEY='fake-local-only',ANTHROPIC_BASE_URL=f'http://127.0.0.1:{server.server_port}',CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',CLAUDE_CODE_MAX_RETRIES='0')
        for k in ['CLAUDECODE','ANTHROPIC_AUTH_TOKEN','CLAUDE_CODE_OAUTH_TOKEN']:env.pop(k,None)
        sid=str(uuid.uuid4()); new=str(uuid.uuid4())
        memory=root/'shared-memory';memory.mkdir();(memory/'MEMORY.md').write_text('SHARED_MEMORY_MARKER: use this across both accounts.')
        plugin=root/'shared-plugin';(plugin/'.claude-plugin').mkdir(parents=True)
        (plugin/'.claude-plugin/plugin.json').write_text(json.dumps({'name':'lab-test-plugin'}))
        skill=plugin/'skills/check';skill.mkdir(parents=True)
        (skill/'SKILL.md').write_text('---\nname: check\ndescription: SHARED_PLUGIN_MARKER\n---\nA local testing skill.')
        base=[shutil.which('claude') or str(Path.home()/'.local/bin/claude'),'-p','ARCHIVE_USER_MARKER','--model','haiku','--tools','Skill','--allowedTools','Skill','--permission-mode','acceptEdits','--permission-prompts','none','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--plugin-dir',str(plugin),'--setting-sources','','--output-format','json','--max-turns','1','--system-prompt-snapshot','off','--exclude-dynamic-system-prompt-sections','--settings',json.dumps({'autoMemoryEnabled':True,'autoMemoryDirectory':str(memory)})]
        env['CLAUDE_CONFIG_DIR']=str(root/'a')
        first=subprocess.run(base+['--session-id',sid],cwd=work,env=env,capture_output=True,text=True,timeout=40)
        print('Initial CLI exit:',first.returncode)
        if first.returncode: raise RuntimeError(first.stderr[-1000:]+' '+first.stdout[-1000:])
        files=list((root/'a/projects').glob('*/'+sid+'.jsonl'))
        if not files:raise RuntimeError('No CLI transcript created')
        env['CLAUDE_CONFIG_DIR']=str(root/'b')
        before=len(requests)
        second=subprocess.run(base+['--session-id',new,'--resume',str(files[0]),'--fork-session'],cwd=work,env=env,capture_output=True,text=True,timeout=40)
        print('Cross-profile fork exit:',second.returncode)
        if second.returncode:raise RuntimeError(second.stderr[-1000:]+' '+second.stdout[-1000:])
        sent=json.dumps(requests[before:])
        if 'SHARED_MEMORY_MARKER' not in sent:raise RuntimeError('Shared memory missing from resumed context')
        if 'SHARED_PLUGIN_MARKER' not in sent:raise RuntimeError('Shared plugin missing from resumed context')
        if 'ARCHIVE_REPLY_MARKER' not in sent:raise RuntimeError('Previous assistant response missing from resumed model context')
        if not list((root/'b/projects').glob('*/'+new+'.jsonl')):raise RuntimeError('Fork did not use requested new session ID')
        print('Verified real CLI resumes account A transcript under account B, sends prior conversation, writes a new session, and loads shared memory plus shared plugin skills. No provider calls: local fake API only.')
finally: server.shutdown();server.server_close()
