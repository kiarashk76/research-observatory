"""Real-browser integration, enabled when local editor build tools are installed."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from labcore.dashboard import make_server
from labcore.store import Store

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT/'runtime/tools/node/bin/node').exists() and (ROOT/'runtime/tools/playwright').exists(),'Local browser tooling is not installed')
class NotesBrowserTests(unittest.TestCase):
    def test_editor_and_collaboration_end_to_end(self):
        with tempfile.TemporaryDirectory(prefix='research-notes-browser-') as folder:
            root=Path(folder);store=Store(root/'runtime/lab.sqlite3')
            (root/'config.yaml').write_text('{}')
            with store.tx() as tx:tx.put('projects',dict(id='p',folder='p',name='Notebook test',purpose='Test',status='active'))
            (root/'projects/p/notes').mkdir(parents=True)
            credentials=root/'runtime/dashboard-access';credentials.mkdir();(credentials/'credentials.json').write_text(json.dumps(dict(username='owner',password='notes-browser-test-password')))
            (root/'frontend').symlink_to(ROOT/'frontend',target_is_directory=True)
            (root/'runtime/tools').symlink_to(ROOT/'runtime/tools',target_is_directory=True)
            (root/'labcore/dashboard').mkdir(parents=True);(root/'labcore/dashboard/notes-vendor').symlink_to(ROOT/'labcore/dashboard/notes-vendor',target_is_directory=True)
            server=make_server(root,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                env=dict(os.environ,NOTES_TEST_BASE='http://127.0.0.1:'+str(server.server_port),NOTES_TEST_ROOT=str(root),NOTES_TEST_PYTHON=sys.executable,PLAYWRIGHT_BROWSERS_PATH=str(ROOT/'runtime/tools/playwright'))
                result=subprocess.run([str(ROOT/'runtime/tools/node/bin/node'),str(ROOT/'tests/notes_browser.mjs')],env=env,cwd=ROOT,capture_output=True,text=True,timeout=150)
                self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
                self.assertEqual(store.all('requests'),[],'Editor operations woke an agent')
                self.assertGreater((root/'notes-test.pdf').stat().st_size,10000)
                from pypdf import PdfReader
                text='\n'.join(page.extract_text() for page in PdfReader(root/'notes-test.pdf').pages)
                separate='\n'.join(page.extract_text() for page in PdfReader(root/'notes-separate-test.pdf').pages)
                self.assertIn('Separate saved content',separate)
                self.assertIn('Renamed experiment',separate)
                self.assertNotIn('Updated first',separate)
                self.assertIn('Formulation',text)
                self.assertIn('Test plot caption',text)
                self.assertNotIn('\\boxed',text)
                layout='\n'.join(page.extract_text() for page in PdfReader(root/'notes-layout-test.pdf').pages)
                self.assertIn('Box content',layout)
                self.assertIn('Header A',layout)
                self.assertNotIn(':::notes-align',layout)
                columns='\n'.join(page.extract_text() for page in PdfReader(root/'notes-columns-test.pdf').pages)
                self.assertIn('Right table',columns)
                self.assertIn('Header A',columns)
                self.assertNotIn(':::notes-column',columns)
                numbered='\n'.join(page.extract_text() for page in PdfReader(root/'notes-numbered-test.pdf').pages)
                self.assertIn('First numbered item',numbered)
                self.assertIn('Second numbered item',numbered)
                for heading in ('Heading level one','Heading level two','Heading level three'):
                    self.assertIn(heading,numbered)
            finally:server.shutdown();server.server_close()


    def test_research_chat_workspace_accounts_and_memory(self):
        import yaml
        with tempfile.TemporaryDirectory(prefix='research-chat-browser-') as folder:
            root=Path(folder);store=Store(root/'runtime/lab.sqlite3')
            accounts=[]
            for account in ('primary','secondary','tertiary'):
                directory=root/'runtime'/account;directory.mkdir(parents=True)
                (directory/'.credentials.json').write_text(json.dumps(dict(claudeAiOauth=dict(accessToken='offline-test',expiresAt=9999999999999))))
                accounts.append(dict(id=account,config_dir=str(directory)))
            (root/'config.yaml').write_text(yaml.safe_dump(dict(claude=dict(bin=str(ROOT/'tests/fixtures/fake_research_chat_cli.py'),accounts=accounts))))
            with store.tx() as tx:
                for pid in ('p','other'):
                    tx.put('projects',dict(id=pid,folder=pid,name='Chat '+pid,status='active'))
                    (root/'projects'/pid/'notes').mkdir(parents=True)
                    reports=root/'projects'/pid/'reports';reports.mkdir()
                    from PIL import Image
                    Image.new('RGB',(80,40),'navy').save(reports/'original.png')
            credentials=root/'runtime/dashboard-access';credentials.mkdir();(credentials/'credentials.json').write_text(json.dumps(dict(username='owner',password='notes-browser-test-password')))
            server=make_server(root,0);threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                env=dict(os.environ,NOTES_TEST_BASE='http://127.0.0.1:'+str(server.server_port),PLAYWRIGHT_BROWSERS_PATH=str(ROOT/'runtime/tools/playwright'))
                result=subprocess.run([str(ROOT/'runtime/tools/node/bin/node'),str(ROOT/'tests/chat_browser.mjs')],env=env,cwd=ROOT,capture_output=True,text=True,timeout=120)
                self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
                self.assertEqual(store.all('requests'),[])
            finally:server.shutdown();server.server_close()
