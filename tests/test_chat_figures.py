import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from labcore.store import Store
from labcore.chat_evidence import Evidence
from labcore.chat_figures import run_plot

class ChatFigureTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);(self.root/'config.yaml').write_text('{}');self.store=Store(self.root/'runtime/lab.sqlite3')
  with self.store.tx() as tx:
   for pid in ('p','other'):tx.put('projects',dict(id=pid,name=pid,folder=pid,status='active'));(self.root/'projects'/pid/'reports').mkdir(parents=True)
   tx.put('research_chats',dict(id='chat',project='p',status='running',current_message='turn'))
  self.csv=self.root/'projects/p/reports/data.csv';self.csv.write_text('x,y\n1,2\n2,4\n3,6\n')
  Image.new('RGB',(80,40),'navy').save(self.root/'projects/p/reports/original.png')
  self.e=Evidence(self.root,'p','chat');self.source=next(k for k,s in self.e.sources.items() if s['path']==self.csv)
 def test_retrieve_figure_creates_inline_attachment(self):
  source=next(k for k,s in self.e.sources.items() if s['path'] and s['path'].name=='original.png')
  content=self.e.call('show_figure',dict(source=source,caption='Original pilot result'))
  attachment=json.loads(content[0]['text'])['attachment'];self.assertEqual(attachment['kind'],'retrieved');self.assertTrue(attachment['url'].startswith('/api/research-chat-asset?'))
  self.assertEqual(len(self.store.get('research_chats','chat')['live_assets']),1)
  self.assertIn(attachment['source_id'],self.e.sources)
  self.assertEqual(self.e.call('view_figure',dict(source=attachment['source_id']))[1]['type'],'image')
 def test_plot_regeneration_changes_labels_and_preserves_originals(self):
  before=self.csv.read_bytes()
  code="""import csv
from pathlib import Path
import matplotlib.pyplot as plt
rows=list(csv.DictReader(open(INPUTS[SOURCE])))
fig,ax=plt.subplots()
ax.plot([float(row['x']) for row in rows],[float(row['y']) for row in rows],label='Revised legend')
ax.set_xlabel('Updated horizontal label');ax.set_ylabel('Measured value');ax.legend()
fig.savefig(Path(OUTPUT_DIR)/'revised.png',dpi=100)
fig.savefig(Path(OUTPUT_DIR)/'revised.pdf')
""".replace('SOURCE',repr(self.source))
  result=json.loads(run_plot(self.e,dict(sources=[self.source],code=code,caption='Revised labels'))[0]['text'])
  self.assertEqual(self.csv.read_bytes(),before);self.assertEqual(len(result['attachments']),2)
  image=next(a for a in result['attachments'] if a['mime']=='image/png');self.assertEqual(image['inputs'][0]['id'],self.source)
  self.assertEqual(result['script']['kind'],'script');self.assertEqual(self.store.all('jobs'),[])
 def test_plot_cannot_use_network_read_credentials_write_inputs_or_spawn(self):
  secret=self.root/'runtime/secret.txt';secret.write_text('private')
  code="""import socket,subprocess,json
from pathlib import Path
checks={}
try:socket.socket();checks['network']=False
except PermissionError:checks['network']=True
try:open(SECRET).read();checks['secret']=False
except PermissionError:checks['secret']=True
try:open(INPUTS[SOURCE],'w').write('bad');checks['original_write']=False
except PermissionError:checks['original_write']=True
try:subprocess.run(['/bin/true']);checks['spawn']=False
except (OSError,RuntimeError):checks['spawn']=True
assert all(checks.values()),checks
Path(OUTPUT_DIR,'checks.json').write_text(json.dumps(checks))
""".replace('SECRET',repr(str(secret))).replace('SOURCE',repr(self.source))
  result=json.loads(run_plot(self.e,dict(sources=[self.source],code=code,caption='Sandbox checks'))[0]['text'])
  asset=self.store.get('research_chat_assets',result['attachments'][0]['id']);self.assertTrue(all(json.loads(Path(asset['path']).read_text()).values()))
  self.assertTrue(self.csv.read_text().startswith('x,y'))
 def test_sources_and_active_conversation_are_enforced(self):
  with self.assertRaises(ValueError):run_plot(self.e,dict(sources=['outside'],code='pass'))
  with self.store.tx() as tx:tx.put('research_chats',dict(id='chat',project='other',status='running'))
  with self.assertRaises(ValueError):run_plot(self.e,dict(sources=[],code='pass'))
 def test_asset_http_requires_login_and_matching_workspace(self):
  import base64,threading,urllib.request,urllib.error
  from labcore.dashboard import make_server
  source=next(k for k,s in self.e.sources.items() if s['path'] and s['path'].name=='original.png')
  result=json.loads(self.e.call('show_figure',dict(source=source))[0]['text']);url=result['attachment']['url']
  credentials=self.root/'runtime/dashboard-access';credentials.mkdir();(credentials/'credentials.json').write_text(json.dumps(dict(username='owner',password='test-password')))
  server=make_server(self.root,0);threading.Thread(target=server.serve_forever,daemon=True).start();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
  base='http://127.0.0.1:'+str(server.server_port)
  with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(base+url)
  self.assertEqual(error.exception.code,401)
  headers={'Authorization':'Basic '+base64.b64encode(b'owner:test-password').decode()}
  response=urllib.request.urlopen(urllib.request.Request(base+url,headers=headers));self.assertTrue(response.read().startswith(b'\x89PNG'))
  with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(urllib.request.Request(base+url.replace('project=p&','project=other&'),headers=headers))
  self.assertEqual(error.exception.code,404)
