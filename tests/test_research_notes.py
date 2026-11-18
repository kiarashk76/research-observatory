"""Notes persistence, human-only writes, permissions and revision conflict checks."""
import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from PIL import Image

from labcore import research_notes as notes
from labcore.dashboard import make_server
from labcore.store import Store


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.store=Store(self.root/'runtime/lab.sqlite3')
        with self.store.tx() as tx:
            for ident in ('p','other'):tx.put('projects',dict(id=ident,folder=ident,name=ident))
        self.session=dict(id='s',project='p',role='supervisor')
    def save(self,text,revision=0):return notes.human_action(self.root,'p',dict(operation='save',revision=revision,markdown=text))
    def proposal(self,**change):
        with self.store.tx() as tx:return notes.propose(self.root,tx,self.session,dict(dict(base_revision=1,mode='replace',replacement='# Notes\nNew text'),**change))
    def test_save_revision_export_and_no_agent_requests(self):
        result=self.save('# Notes\nHuman text')
        self.assertEqual(result['note']['revision'],1)
        self.assertEqual(self.store.all('requests'),[])
        self.assertIn('Human text',(self.root/'projects/p'/notes.PATH).read_text())
        self.save('# Notes\nMore text',1)
        self.assertEqual(self.store.get('research_note_revisions','p:1')['markdown'],'# Notes\nHuman text')
        with self.assertRaises(notes.Conflict):self.save('Another tab',1)
        self.assertEqual(self.store.get('research_notes','p')['revision'],2)
    def test_proposals_require_acceptance_and_reject_stale_revisions(self):
        self.save('# Notes\nHuman text');proposal=self.proposal()
        self.assertIn('Human text',self.store.get('research_notes','p')['markdown'])
        self.save('# Notes\nNewer human edit',1)
        with self.assertRaises(notes.Conflict):notes.human_action(self.root,'p',dict(operation='accept',revision=2,proposal_id=proposal['id']))
        self.assertIn('Newer human edit',self.store.get('research_notes','p')['markdown'])
    def test_older_section_proposal_accepts_after_unrelated_edits_and_offsets_change(self):
        self.save('## Motivation\nOld\n\n## Results\nOriginal result')
        proposal=self.proposal(section='Motivation',replacement='## Motivation\nImproved')
        outside='## Introduction\nAdded elsewhere\n\n'
        self.save(outside+'## Motivation\nOld\n\n## Results\nNew human result',1)
        with self.store.tx() as tx:
            displayed=notes.view(tx,'p')['proposals'][0]
            self.assertTrue(displayed['can_accept']);self.assertEqual(displayed['accept_blocked_reason'],'')
        result=notes.human_action(self.root,'p',dict(operation='accept',revision=2,proposal_id=proposal['id']))
        self.assertEqual(result['note']['revision'],3)
        self.assertEqual(result['note']['markdown'],outside+'## Motivation\nImproved\n\n## Results\nNew human result')
        stored=self.store.get('research_note_proposals',proposal['id'])
        self.assertEqual(stored['base_revision'],1);self.assertEqual(stored['accepted_revision'],3)
        self.assertEqual(self.store.all('requests'),[])

    def test_accepting_separate_sections_keeps_the_other_suggestion_usable(self):
        self.save('## Motivation\nOld\n\n## Results\nOld result')
        first=self.proposal(section='Motivation',replacement='## Motivation\nImproved')
        second=self.proposal(section='Results',replacement='## Results\nVerified result')
        competing=self.proposal(section='Motivation',replacement='## Motivation\nAlternative')
        notes.human_action(self.root,'p',dict(operation='accept',revision=1,proposal_id=first['id']))
        with self.store.tx() as tx:
            displayed={p['id']:p for p in notes.view(tx,'p')['proposals']}
            self.assertTrue(displayed[second['id']]['can_accept'])
            self.assertFalse(displayed[competing['id']]['can_accept'])
        result=notes.human_action(self.root,'p',dict(operation='accept',revision=2,proposal_id=second['id']))
        self.assertIn('Improved',result['note']['markdown']);self.assertIn('Verified result',result['note']['markdown'])
        with self.assertRaises(notes.Conflict):notes.human_action(self.root,'p',dict(operation='accept',revision=3,proposal_id=competing['id']))

    def test_changed_renamed_missing_and_whitespace_edited_sections_stay_blocked(self):
        original='## Motivation\nOld\n\n### Detail\nNested text\n\n## Results\nOriginal'
        self.save(original);proposal=self.proposal(section='Motivation',replacement='## Motivation\nImproved')
        for changed in [original.replace('Old','Human edit'),original.replace('Motivation','Renamed'),
                        '## Results\nOnly this remains',original.replace('Nested text','Changed nested text'),
                        original.replace('Old','Old ')]:
            with self.subTest(changed=changed):
                note=dict(self.store.get('research_notes','p'),markdown=changed,revision=2)
                self.assertIsNotNone(notes.proposal_conflict(note,proposal))
                with self.store.tx() as tx:tx.put('research_notes',note)
                with self.assertRaises(notes.Conflict):notes.human_action(self.root,'p',dict(operation='accept',revision=2,proposal_id=proposal['id']))
                self.assertEqual(self.store.get('research_notes','p')['markdown'],changed)
                self.assertEqual(self.store.get('research_note_proposals',proposal['id'])['status'],'pending')

    def test_exact_source_comparison_does_not_bypass_revision_or_permission_checks(self):
        self.save('## Motivation\nOld\n\n## Results\nOriginal')
        proposal=self.proposal(section='Motivation',replacement='## Motivation\nImproved')
        permission=notes.human_action(self.root,'p',dict(operation='grant',revision=1,section='Motivation'))['grants'][0]
        self.save('## Motivation\nOld\n\n## Results\nHuman result',1)
        with self.assertRaises(notes.Conflict):notes.human_action(self.root,'p',dict(operation='accept',revision=1,proposal_id=proposal['id']))
        with self.store.tx() as tx:
            with self.assertRaisesRegex(ValueError,'permission'):notes.accept(tx,'p',proposal['id'],2,grant_id=permission['id'])
        self.assertEqual(self.store.get('research_note_grants',permission['id'])['status'],'active')
        legacy=dict(proposal);legacy.pop('before')
        self.assertIsNotNone(notes.proposal_conflict(self.store.get('research_notes','p'),legacy))

    def test_whole_document_proposal_requires_exact_document_match(self):
        self.save('# Notes\nOriginal');proposal=self.proposal()
        self.save('# Notes\nOriginal\nMore',1)
        self.assertIsNotNone(notes.proposal_conflict(self.store.get('research_notes','p'),proposal))
        self.save('# Notes\nOriginal',2)
        result=notes.human_action(self.root,'p',dict(operation='accept',revision=3,proposal_id=proposal['id']))
        self.assertIn('New text',result['note']['markdown'])

    def test_scoped_one_time_permission_preserves_other_sections(self):
        self.save('## Motivation\nOld\n\n## Results\nLeave this alone')
        section=notes.sections(self.store.get('research_notes','p')['markdown'])[0]
        with self.store.tx() as tx:
            permission=notes.grant(tx,'p',1,section['key'])
            proposal=notes.propose(self.root,tx,self.session,dict(base_revision=1,section=section['key'],mode='replace',replacement='## Motivation\nImproved'))
            notes.accept(tx,'p',proposal['id'],1,author='supervisor',grant_id=permission['id'])
        self.assertIn('## Results\nLeave this alone',self.store.get('research_notes','p')['markdown'])
        self.assertEqual(self.store.get('research_note_grants',permission['id'])['status'],'used')
        with self.store.tx() as tx:
            proposal=notes.propose(self.root,tx,self.session,dict(base_revision=2,section=section['key'],mode='replace',replacement='## Motivation\nAgain'))
            with self.assertRaises(ValueError):notes.accept(tx,'p',proposal['id'],2,grant_id=permission['id'])
    def test_scope_escape_invalid_grant_and_cross_project_edits_are_rejected(self):
        self.save('## Motivation\nOld\n\n## Results\nHuman results')
        for replacement in ('## Different\nOops','## Motivation\nNew\n## Results\nReplaced'):
            with self.store.tx() as tx,self.assertRaises(ValueError):notes.propose(self.root,tx,self.session,dict(base_revision=1,section='Motivation',mode='replace',replacement=replacement))
        proposal=self.proposal()
        with self.store.tx() as tx:
            permission=notes.grant(tx,'p',1,'Motivation')
            with self.assertRaises(ValueError):notes.accept(tx,'p',proposal['id'],1,grant_id=permission['id'])
        with self.assertRaises(ValueError):notes.human_action(self.root,'other',dict(operation='accept',revision=0,proposal_id=proposal['id']))
    def test_revoke_and_restore_preserve_history(self):
        self.save('Original');permission=notes.human_action(self.root,'p',dict(operation='grant',revision=1))['grants'][0]
        notes.human_action(self.root,'p',dict(operation='revoke',grant_id=permission['id']))
        self.assertEqual(self.store.get('research_note_grants',permission['id'])['status'],'revoked')
        self.save('Later',1)
        result=notes.human_action(self.root,'p',dict(operation='restore',revision=2,restore_revision=1))
        self.assertEqual(result['note']['revision'],3);self.assertEqual(result['note']['markdown'],'Original')
        self.assertEqual(self.store.get('research_note_revisions','p:2')['markdown'],'Later')
    def test_figures_are_validated_project_scoped_and_exported(self):
        image=io.BytesIO();Image.new('RGB',(2,2),'white').save(image,format='PNG')
        result=notes.human_action(self.root,'p',dict(operation='upload',data=base64.b64encode(image.getvalue()).decode(),name='Figure'))
        asset=result['asset'];self.save('![A figure]('+asset['relative']+')')
        self.assertTrue((self.root/'projects/p'/asset['relative']).exists())
        with self.assertRaises(ValueError):notes.human_action(self.root,'p',dict(operation='upload',data=base64.b64encode(b'<svg onload="alert(1)">').decode()))
    def test_section_references_are_unambiguous_and_ignore_code(self):
        text='## Same\nOne\n```python\n# Not a section\n```\n## Same\nTwo'
        values=notes.sections(text);self.assertEqual(len(values),2);self.assertNotEqual(values[0]['key'],values[1]['key'])
        with self.assertRaises(ValueError):notes.resolve_section(text,'Same')
    def test_http_notes_writes_require_owner_login_even_on_localhost(self):
        folder=self.root/'runtime/dashboard-access';folder.mkdir();(folder/'credentials.json').write_text(json.dumps(dict(username='owner',password='only-the-human-knows-this-password')))
        server=make_server(self.root,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url='http://127.0.0.1:'+str(server.server_port)+'/api/research-notes/p'
        data=json.dumps(dict(operation='save',revision=0,markdown='Human draft')).encode()
        request=urllib.request.Request(url,data=data,headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
        self.assertEqual(error.exception.code,401)
        auth='Basic '+base64.b64encode(b'owner:only-the-human-knows-this-password').decode()
        request.add_header('Authorization',auth)
        with urllib.request.urlopen(request) as response:self.assertEqual(json.load(response)['note']['revision'],1)
        request=urllib.request.Request(url,data=data,headers={'Content-Type':'application/json','Authorization':auth})
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
        self.assertEqual(error.exception.code,409)
        request.add_header('Origin','https://evil.example')
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
        self.assertEqual(error.exception.code,403)
        with urllib.request.urlopen(url+'?metadata=1') as response:self.assertEqual(json.load(response)['revision'],1)

    def test_named_notes_history_permissions_and_legacy_isolation(self):
        self.save('# Original\nLegacy content')
        created=notes.human_action(self.root,'p',dict(operation='create',title='Experiment log'))
        ident=created['note']['id']
        self.assertEqual(len(created['notes']),2)
        def action(operation,**kwargs):
            return notes.human_action(self.root,'p',dict(operation=operation,note_id=ident,**kwargs))
        action('save',revision=0,markdown='# Results\nFirst')
        permission=action('grant',revision=1)['grants'][0]
        with self.store.tx() as tx:
            proposal=notes.propose(self.root,tx,self.session,dict(note_id=ident,base_revision=1,mode='replace',replacement='# Results\nAgent'))
            with self.assertRaises(ValueError):notes.accept(tx,'p',proposal['id'],1)
            with self.assertRaises(ValueError):notes.propose(self.root,tx,self.session,dict(base_revision=1,mode='append',replacement='Ambiguous'))
        self.save('# Original\nChanged elsewhere',1)
        with self.store.tx() as tx:
            self.assertFalse(notes.view(tx,ident)['grants'][0]['stale'])
            notes.accept(tx,ident,proposal['id'],1,grant_id=permission['id'])
        renamed=action('rename',revision=2,title='Renamed log')
        self.assertEqual(renamed['note']['id'],ident)
        self.assertEqual(renamed['note']['title'],'Renamed log')
        self.assertEqual([r['revision'] for r in renamed['history']],[2,1,0])
        restored=action('restore',revision=2,restore_revision=1)
        self.assertEqual(restored['note']['markdown'],'# Results\nFirst')
        self.assertEqual(self.store.get('research_notes','p')['markdown'],'# Original\nChanged elsewhere')
        self.assertTrue((self.root/'projects/p/notes/research-notes'/f'{ident}.md').exists())
        for operation,extra in [('save',dict(revision=3,markdown='Wrong project')),('restore',dict(revision=3,restore_revision=1)),('upload',dict(data=''))]:
            with self.assertRaises(ValueError):notes.human_action(self.root,'other',dict(operation=operation,note_id=ident,**extra))
        with self.assertRaises(ValueError):action('revoke',grant_id=notes.human_action(self.root,'p',dict(operation='grant',revision=2))['grants'][0]['id'])
        self.assertEqual(self.store.all('requests'),[])

    def test_delete_notes_revokes_access_preserves_history_and_replaces_last(self):
        self.save('Default source')
        ident=notes.human_action(self.root,'p',dict(operation='create',title='Disposable'))['note']['id']
        notes.human_action(self.root,'p',dict(operation='save',note_id=ident,revision=0,markdown='Separate source'))
        permission=notes.human_action(self.root,'p',dict(operation='grant',note_id=ident,revision=1))['grants'][0]
        with self.store.tx() as tx:
            proposal=notes.propose(self.root,tx,self.session,dict(note_id=ident,base_revision=1,mode='append',replacement='Pending'))
        with self.assertRaises(notes.Conflict):notes.human_action(self.root,'p',dict(operation='delete',note_id=ident,revision=0))
        result=notes.human_action(self.root,'p',dict(operation='delete',note_id=ident,revision=1))
        self.assertEqual(result['note']['id'],'p')
        self.assertEqual(len(result['notes']),1)
        self.assertEqual(self.store.get('research_note_grants',permission['id'])['status'],'revoked')
        self.assertEqual(self.store.get('research_note_proposals',proposal['id'])['status'],'pending')
        self.assertEqual(self.store.get('research_note_revisions',ident+':1')['markdown'],'Separate source')
        self.assertFalse((self.root/'projects/p/notes/research-notes'/f'{ident}.md').exists())
        for operation in ('save','rename','restore','grant'):
            with self.assertRaises(ValueError):notes.human_action(self.root,'p',dict(operation=operation,note_id=ident,revision=1,markdown='resurrect',title='resurrect',restore_revision=1))
        last=notes.human_action(self.root,'p',dict(operation='delete',note_id='p',revision=1))
        self.assertNotEqual(last['note']['id'],'p')
        self.assertEqual(last['note']['markdown'],'')
        self.assertEqual(len(last['notes']),1)
        self.assertFalse((self.root/'projects/p'/notes.PATH).exists())
        with self.store.tx() as tx:
            self.assertEqual(notes.select(tx,'p'),last['note']['id'])
            self.assertEqual(notes.target(tx,'p'),last['note']['id'])
            with self.assertRaises(ValueError):notes.select(tx,'other',last['note']['id'])
        self.assertEqual(self.store.all('requests'),[])

    def test_archive_preserves_draft_history_and_restores_without_permissions(self):
        self.save('Saved original')
        result=notes.human_action(self.root,'p',dict(operation='delete',note_id='p',revision=1,markdown='Unsaved latest content'))
        self.assertEqual(result['archived_notes'][0]['note_id'],'p')
        viewed=notes.human_action(self.root,'p',dict(operation='view_archive',archived_note_id='p'))
        self.assertEqual(viewed['note']['markdown'],'Unsaved latest content')
        with self.assertRaises(ValueError):notes.human_action(self.root,'other',dict(operation='unarchive',archived_note_id='p'))
        restored=notes.human_action(self.root,'p',dict(operation='unarchive',archived_note_id='p'))
        self.assertEqual(restored['note']['id'],'p')
        self.assertEqual(restored['note']['revision'],2)
        self.assertEqual(len(restored['history']),3)
        self.assertEqual(restored['archived_notes'],[])
        self.assertIn('Unsaved latest content',(self.root/'projects/p'/notes.PATH).read_text())
        empty=notes.human_action(self.root,'p',dict(operation='create',title='Empty note'))['note']['id']
        removed=notes.human_action(self.root,'p',dict(operation='delete',note_id=empty,revision=0,markdown='  '))
        self.assertFalse(any(n['note_id']==empty for n in removed['archived_notes']))
        self.assertEqual(self.store.get('research_notes',empty)['title'],'')
        with self.store.tx() as tx:
            self.assertFalse(any(notes.belongs(r,dict(id=empty,project='p')) for r in tx.all('research_note_revisions')))
        notes.human_action(self.root,'p',dict(operation='save',revision=2,markdown=''))
        self.assertTrue(notes.human_action(self.root,'p',dict(operation='delete',revision=3))['archived_notes'])

    def test_named_notes_chat_search_and_proposals(self):
        from labcore.chat_evidence import Evidence
        self.save('Default evidence')
        ident=notes.human_action(self.root,'p',dict(operation='create',title='Ablation log'))['note']['id']
        notes.human_action(self.root,'p',dict(operation='save',note_id=ident,revision=0,markdown='Unique ablation evidence'))
        with self.store.tx() as tx:
            tx.put('research_chats',dict(id='chat',project='p',status='running'))
        evidence=Evidence(self.root,'p','chat')
        source='notes:'+ident
        details=json.loads(evidence.call('read_evidence',dict(source=source))[0]['text'])
        self.assertEqual(details['note_id'],ident)
        self.assertEqual(details['title'],'Ablation log')
        self.assertIn('Unique ablation',details['text'])
        result=json.loads(evidence.call('propose_notes_edit',dict(note_id=ident,base_revision=1,mode='append',replacement='Suggested',summary='Addition'))[0]['text'])
        with self.store.tx() as tx:
            self.assertEqual(len(notes.view(tx,ident)['proposals']),1)
            self.assertEqual(len(notes.view(tx,'p')['proposals']),0)
        self.assertEqual(result['url'],'#notes/'+ident)
        with self.assertRaises(ValueError):Evidence(self.root,'other').call('read_evidence',dict(source=source))

import test_platform as fixtures
import subprocess
import sys
from unittest.mock import patch


class NotesAgentAuthorityTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp
    cleanup_processes=fixtures.PlatformTests.cleanup_processes
    task=fixtures.PlatformTests.task
    session=fixtures.PlatformTests.session

    def prepare_notes(self):
        ident=self.channel['project']
        notes.human_action(self.root,ident,dict(operation='save',revision=0,markdown='## Motivation\nHuman idea\n\n## Results\nProtected result'))
        return ident,dict(id='supervisor',project=ident,role='supervisor',channel='C1',thread='100.1')

    def test_agent_actions_require_permission_and_generic_note_write_is_blocked(self):
        ident,supervisor=self.prepare_notes()
        with self.engine.store.tx() as tx:
            with self.assertRaisesRegex(ValueError,'human-owned'):
                self.engine._action(tx,supervisor,None,dict(type='write_note',path=notes.PATH,text='Bypass'))
        change=dict(base_revision=1,mode='append',section='Motivation',replacement='Agent addition')
        with self.assertRaisesRegex(ValueError,'permission'):
            with self.engine.store.tx() as tx:self.engine._action(tx,supervisor,None,dict(type='edit_research_notes',notes_change=change))
        self.assertEqual(self.engine.store.all('research_note_proposals'),[])
        with self.engine.store.tx() as tx:
            permission=notes.grant(tx,ident,1,'Motivation')
            self.engine._action(tx,supervisor,None,dict(type='edit_research_notes',grant_id=permission['id'],notes_change=change))
        saved=self.engine.store.get('research_notes',ident)
        self.assertIn('Agent addition',saved['markdown']);self.assertIn('## Results\nProtected result',saved['markdown'])
        self.assertEqual(self.engine.store.get('research_note_grants',permission['id'])['status'],'used')

    def test_specialists_can_propose_but_cannot_directly_apply_edits(self):
        ident,supervisor=self.prepare_notes();specialist=dict(supervisor,role='literature')
        change=dict(base_revision=1,mode='comment',comment='Please clarify the hypothesis.',section='Motivation')
        with self.engine.store.tx() as tx:self.engine._action(tx,specialist,None,dict(type='propose_research_notes',notes_change=change))
        self.assertEqual(self.engine.store.get('research_notes',ident)['revision'],1)
        self.assertEqual(len(self.engine.store.all('research_note_proposals')),1)
        with self.assertRaisesRegex(ValueError,'Only the supervisor'):
            with self.engine.store.tx() as tx:self.engine._action(tx,specialist,None,dict(type='edit_research_notes',notes_change=change,grant_id='anything'))

    def test_slack_one_time_permission_does_not_wake_a_model(self):
        ident,_=self.prepare_notes();before=len(self.engine.store.all('requests'))
        event=dict(channel='C1',ts='999.1',user='human',text='!notes allow Motivation')
        self.bridge.message(event);self.bridge.message(event)
        self.assertEqual(len(self.engine.store.all('research_note_grants')),1)
        self.assertEqual(len(self.engine.store.all('requests')),before)

    def test_named_note_slack_permissions_and_agent_actions(self):
        pid,_=self.prepare_notes()
        ident=notes.human_action(self.root,pid,dict(operation='create',title='Experiment log'))['note']['id']
        notes.human_action(self.root,pid,dict(operation='save',note_id=ident,revision=0,markdown='# Results\nOriginal'))
        self.bridge.message(dict(channel='C1',ts='1000.1',user='human',text='!notes allow all'))
        self.assertEqual(len(self.engine.store.all('research_note_grants')),0)
        self.bridge.message(dict(channel='C1',ts='1000.2',user='human',text='!notes allow Experiment log | all'))
        permission=self.engine.store.all('research_note_grants')[0]
        self.assertEqual(permission['note_id'],ident)
        supervisor=self.session('supervisor')
        with self.engine.store.tx() as tx:
            self.engine._action(tx,supervisor,None,dict(type='edit_research_notes',grant_id=permission['id'],notes_change=dict(note_id=ident,base_revision=1,mode='append',replacement='Authorized addition')))
        self.engine.refresh_views(pid)
        self.assertIn('Authorized addition',self.engine.store.get('research_notes',ident)['markdown'])
        self.assertNotIn('Authorized addition',self.engine.store.get('research_notes',pid)['markdown'])
        project=self.engine.store.get('projects',pid)
        self.assertIn('Authorized addition',(self.root/'projects'/project['folder']/'notes/research-notes'/f'{ident}.md').read_text())

    def test_sandbox_can_read_notes_but_not_write_them_or_read_the_password(self):
        ident,_=self.prepare_notes();task=self.task();session=self.session('engineer',task)
        secret=self.root/'runtime/dashboard-access/credentials.json';secret.parent.mkdir();secret.write_text('{"password":"not-visible-to-agents"}')
        project=self.engine.store.get('projects',ident);path=self.root/'projects'/project['folder']/notes.PATH
        script='''import json\nfrom pathlib import Path\nresult={}\npath=Path(%r)\nresult['read']='Human idea' in path.read_text()\ntry:path.write_text('unauthorized');result['write']=True\nexcept PermissionError:result['write']=False\ntry:Path(%r).read_text();result['password']=True\nexcept PermissionError:result['password']=False\nprint(json.dumps(result))\n'''%(str(path),str(secret))
        env=self.runtime.runner.environment(session['workspace'])
        command=self.runtime.runner.sandbox_command([sys.executable,'-c',script],session,env)
        result=subprocess.run(command,cwd=session['workspace'],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),dict(read=True,write=False,password=False))
