import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from labcore.store import Store
from labcore.research_chat import ChatService,view,save_memory,memory
from labcore.chat_evidence import Evidence
from labcore.dashboard import make_server
import urllib.request
import urllib.error
import base64

class ResearchChatTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  (self.root/'config.yaml').write_text('claude:\n  accounts:\n    - id: primary\n      config_dir: runtime/auth\n')
  self.store=Store(self.root/'runtime/lab.sqlite3');self.service=ChatService(self.root)
  with self.store.tx() as tx:
   for pid in ['p','other']:
    tx.put('projects',dict(id=pid,name=pid,folder=pid,status='active'))
    tx.put('research_notes',dict(id=pid,project=pid,markdown=pid+' secret notes',revision=1))
    tx.put('digests',dict(id=pid,project=pid,title=pid+' papers',text=pid+' digest evidence'))
    tx.put('studies',dict(id=pid,study_id=pid,project=pid,title=pid+' study'))
    folder=self.root/'projects'/pid/'reports';folder.mkdir(parents=True);(folder/'report.md').write_text(pid+' report')
 def test_workspace_conversation_and_memory_are_isolated(self):
  a=self.service.action('p',{'operation':'new'})['conversation'];b=self.service.action('other',{'operation':'new'})['conversation']
  self.assertEqual(len(view(self.root,'p')['conversations']),1)
  with self.assertRaises(ValueError):view(self.root,'other',a)
  with self.assertRaises(ValueError):self.service.action('p',dict(operation='stop',conversation=b))
  self.service.action('p',dict(operation='memory',text='My goals',revision=0))
  self.assertEqual(view(self.root,'p')['memory']['text'],'My goals');self.assertEqual(view(self.root,'other')['memory']['text'],'')
  with self.assertRaises(ValueError):self.service.action('p',dict(operation='memory',text='Stale overwrite',revision=0))
 def test_send_only_explicit_actions_launch_and_no_supervisor_work(self):
  view(self.root,'p')
  with patch.object(ChatService,'run') as run:
   self.service.action('p',dict(operation='new'));self.assertFalse(run.called)
   c=self.service.action('p',dict(operation='send',text='Read the plots',request_id='human-message',model='sonnet',effort='medium',account='primary'))['conversation']
   for _ in range(100):
    if run.called:break
    import time;time.sleep(.001)
   self.assertTrue(run.called)
   with self.assertRaises(ValueError):self.service.action('p',dict(operation='send',conversation=c,text='Duplicate while busy'))
  self.assertEqual(self.store.all('requests'),[]);self.assertEqual(self.store.all('tasks'),[]);self.assertEqual(self.store.all('jobs'),[])
 def test_sources_cannot_cross_workspace_or_follow_file_symlinks(self):
  (self.root/'projects/p/reports/escape.md').symlink_to(self.root/'projects/other/reports/report.md')
  one=Evidence(self.root,'p');other=Evidence(self.root,'other')
  matches=json.loads(one.call('search_evidence',{'query':''})[0]['text'])['matches']
  self.assertTrue(any(s['id']=='notes' for s in matches));self.assertFalse(any('other' in json.dumps(s) for s in matches))
  self.assertEqual(json.loads(one.call('read_evidence',{'source':'notes'})[0]['text'])['text'],'p secret notes')
  forbidden=next(k for k in other.sources if k.startswith('file:'))
  with self.assertRaises(ValueError):one.call('read_evidence',dict(source=forbidden))
  self.assertFalse(any('escape.md' in s['title'] for s in matches))
 def test_interrupted_chat_does_not_retry_and_keeps_history(self):
  with self.store.tx() as tx:tx.put('research_chats',dict(id='c',project='p',title='saved',status='running',owner_pid=-1,owner_identity='gone'))
  with patch.object(ChatService,'run') as run:
   data=view(self.root,'p');self.assertEqual(data['conversation']['status'],'interrupted');run.assert_not_called()
 def test_chat_http_requires_human_auth_and_rejects_cross_site(self):
  authdir=self.root/'runtime/dashboard-access';authdir.mkdir();(authdir/'credentials.json').write_text(json.dumps(dict(username='owner',password='test-password')))
  server=make_server(self.root,0);threading.Thread(target=server.serve_forever,daemon=True).start();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
  url='http://127.0.0.1:'+str(server.server_port)+'/api/research-chat/p'
  with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(url)
  self.assertEqual(error.exception.code,401)
  headers={'Authorization':'Basic '+base64.b64encode(b'owner:test-password').decode(),'Content-Type':'application/json'}
  request=urllib.request.Request(url,data=b'{"operation":"new"}',headers=headers)
  self.assertEqual(urllib.request.urlopen(request).status,200)
  headers['Origin']='https://untrusted.example'
  with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(urllib.request.Request(url,data=b'{"operation":"new"}',headers=headers))
  self.assertEqual(error.exception.code,403)

 def test_pdf_page_text_and_plot_rendering_are_available(self):
  from pypdf import PdfWriter
  folder=self.root/'projects/p/reports';writer=PdfWriter();writer.add_blank_page(width=200,height=100)
  with (folder/'plot.pdf').open('wb') as target:writer.write(target)
  evidence=Evidence(self.root,'p');source=next(k for k,s in evidence.sources.items() if s['title'].endswith('plot.pdf'))
  text=json.loads(evidence.call('read_evidence',dict(source=source))[0]['text']);self.assertEqual(text['pages'],1)
  image=evidence.call('view_figure',dict(source=source))[1];self.assertEqual(image['mimeType'],'image/png');self.assertTrue(base64.b64decode(image['data']).startswith(b'\x89PNG'))
  with self.assertRaises(ValueError):evidence.call('view_figure',dict(source=source,page=2))
 def test_replayed_send_cannot_start_a_second_model_request(self):
  with patch.object(ChatService,'run') as run:
   first=self.service.action('p',dict(operation='send',text='Question',request_id='once'))
   second=self.service.action('p',dict(operation='send',text='Question',request_id='once'))
   self.assertEqual(first,second)

 def test_chat_proposes_notes_but_only_human_can_apply(self):
  from labcore import research_notes as notes
  with self.store.tx() as tx:
   tx.put('research_chats',dict(id='chat',project='p',status='running'))
  evidence=Evidence(self.root,'p','chat')
  details=json.loads(evidence.call('read_evidence',dict(source='notes'))[0]['text'])
  self.assertEqual(details['revision'],1)
  change=dict(base_revision=1,mode='append',replacement='Suggested explanation.',summary='Clarify the notes')
  result=json.loads(evidence.call('propose_notes_edit',change)[0]['text'])
  self.assertEqual(self.store.get('research_notes','p')['markdown'],'p secret notes')
  self.assertEqual(self.store.get('research_note_proposals',result['proposal'])['status'],'pending')
  repeated=json.loads(evidence.call('propose_notes_edit',change)[0]['text']);self.assertEqual(result['proposal'],repeated['proposal'])
  with self.assertRaises(ValueError):Evidence(self.root,'other','chat').call('propose_notes_edit',change)
  with self.assertRaises(ValueError):evidence.call('propose_notes_edit',dict(change,base_revision=0))
  with self.store.tx() as tx:
   notes.accept(tx,'p',result['proposal'],1)
  self.assertIn('Suggested explanation.',self.store.get('research_notes','p')['markdown'])
  with self.assertRaises(ValueError):evidence.call('propose_notes_edit',change)
 def test_complete_literature_note_and_registered_text_are_accessible(self):
  folder=self.root/'workspaces/p/literature';folder.mkdir(parents=True);(folder/'paper.txt').write_text('Actual full paper text')
  with self.store.tx() as tx:
   tx.put('papers',dict(canonical_id='test-paper',title='Paper',note='Detailed literature explanation',source_file='paper.txt',task='lit-task'),key='test-paper')
   tx.put('paper_links',dict(id='p:paper',project='p',paper='test-paper'))
   tx.put('sessions',dict(id='lit',project='p',task='lit-task',workspace=str(folder)))
  evidence=Evidence(self.root,'p')
  note=json.loads(evidence.call('read_evidence',dict(source='paper:test-paper'))[0]['text'])
  self.assertIn('Detailed literature explanation',note['text'])
  text=json.loads(evidence.call('read_evidence',dict(source='papertext:test-paper'))[0]['text'])
  self.assertEqual(text['text'],'Actual full paper text')
  with self.assertRaises(ValueError):Evidence(self.root,'other').call('read_evidence',dict(source='papertext:test-paper'))

 def test_delete_chat_keeps_workspace_memory_and_evidence(self):
  cid=self.service.action('p',dict(operation='new'))['conversation']
  self.service.action('p',dict(operation='memory',text='Keep this preference',revision=0))
  folder=self.root/'runtime/research-chat/p'/cid;folder.mkdir(parents=True);(folder/'history.jsonl').write_text('private chat')
  with self.store.tx() as tx:tx.put('research_chat_messages',dict(id='m',project='p',conversation=cid,role='user',text='Delete me'))
  with self.assertRaises(ValueError):self.service.action('other',dict(operation='delete',conversation=cid))
  self.service.action('p',dict(operation='delete',conversation=cid))
  self.assertEqual(view(self.root,'p')['conversations'],[]);self.assertEqual(self.store.all('research_chat_messages'),[]);self.assertFalse(folder.exists())
  self.assertEqual(view(self.root,'p')['memory']['text'],'Keep this preference');self.assertEqual(self.store.get('research_notes','p')['markdown'],'p secret notes')
  with self.assertRaises(ValueError):view(self.root,'p',cid)
 def test_cannot_delete_a_running_chat(self):
  with self.store.tx() as tx:tx.put('research_chats',dict(id='running',project='p',status='running',owner_pid=__import__('os').getpid(),owner_identity=__import__('labcore.runner',fromlist=['identity']).identity(__import__('os').getpid())))
  with self.assertRaises(ValueError):self.service.action('p',dict(operation='delete',conversation='running'))

 def test_partial_reply_is_preserved_without_replacing_memory(self):
  from labcore.chat_stream import StreamProgress
  with self.store.tx() as tx:tx.put('research_chats',dict(id='partial',project='p',status='running'))
  progress=StreamProgress();progress.reply='Unfinished but useful text'
  self.service.save_partial(dict(id='partial',project='p',model='sonnet',effort='medium'),dict(id='message'),progress,'primary')
  message=self.store.get('research_chat_messages','message:partial')
  self.assertTrue(message['incomplete']);self.assertEqual(message['text'],progress.reply);self.assertEqual(view(self.root,'p')['memory']['text'],'')
