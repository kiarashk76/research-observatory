"""Scheduler publication bounds; no provider calls or research jobs."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from labcore.config import load
from labcore.store import Store
from labcore import documentation_policy as policy, delegation


class DocumentationPolicyTests(unittest.TestCase):
    def test_selecting_next_study_activates_prequeued_unstarted_work_only(self):
        with self.store.tx() as tx:
            study=tx.get('studies','p:one');study['documentation']='complete';tx.put('studies',study)
            tx.put('tasks',dict(self.task,id='future',study_id='two',document_cycle=None,status='queued',attempts=0))
            tx.put('tasks',dict(self.task,id='retired',study_id='two',document_cycle=None,status='cancelled',attempts=0))
            policy.select_focus(tx,'p','two',root=self.root)
            cycle=tx.get('documentation_focus','p')['cycle']
            self.assertEqual(tx.get('tasks','future')['document_cycle'],cycle)
            self.assertIsNone(tx.get('tasks','retired')['document_cycle'])

    def test_later_correction_is_not_silently_replaced_by_completed_phase(self):
        from tests.test_platform import PlatformTests
        fixture=PlatformTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.cfg['documentation']=self.cfg['documentation']
        project=fixture.channel['project'];session=dict(role='supervisor',project=project,channel='C1')
        action=dict(role='literature',model='sonnet',effort='medium',work_kind='documentation',study_id='study',
            document_phase='revise',document_kind='report',goal='Correct figure one',acceptance='Corrected PDF',key='fix-one')
        with fixture.engine.store.tx() as tx:
            tx.put('studies',dict(id=project+':study',project=project,study_id='study'))
            policy.select_focus(tx,project,'study')
            first=fixture.engine.create_task(tx,session,action);first['status']='accepted';tx.put('tasks',first)
            second=fixture.engine.create_task(tx,session,dict(action,key='fix-two',goal='Correct a different reference'))
            self.assertNotEqual(first['id'],second['id'])
            self.assertEqual(second['status'],'queued')
            retry=fixture.engine.create_task(tx,session,dict(action,key='fix-two',goal='Correct a different reference'))
            self.assertEqual(retry['id'],second['id'])

    def test_pair_format_accepts_each_separate_pdf_and_rejects_combined_report(self):
        self.cfg['documentation']['report_format']='legacy_pair'
        self.assertIsNone(self.issue(dict(self.task,document_kind='paper')))
        self.assertIsNone(self.issue(dict(self.task,document_kind='detailed')))
        self.assertIn('configured report format',self.issue())

    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.store=Store(self.root/'db')
        self.cfg=load();self.cfg['documentation']['report_format']='single_with_appendix';self.engine=SimpleNamespace(cfg=self.cfg,root=self.root,store=self.store)
        self.task=dict(id='t',project='p',study_id='one',work_kind='documentation',document_phase='draft',
                       document_kind='report',role='literature',model='sonnet',effort='medium',session_minutes=5,
                       budget=dict(minutes=15,sessions=3,children=0,output_tokens=25000),resources=dict(max_gpus=0))
        base=self.root/'projects/p/reports';base.mkdir(parents=True)
        file=base/'technical.json';file.write_text('saved checked technical record')
        stat=file.stat()
        with self.store.tx() as tx:
            tx.put('projects',dict(id='p',folder='p'))
            tx.put('studies',dict(id='p:one',study_id='one',documentation='incomplete',checks=dict(
                files=dict(technical_record=dict(path='reports/technical.json',size=stat.st_size,mtime_ns=stat.st_mtime_ns)),issues=[])))
            tx.put('studies',dict(id='p:two',study_id='two',documentation='incomplete'))
            policy.select_focus(tx,'p','one',root=self.root)
            self.task['document_cycle']=tx.get('documentation_focus','p')['cycle']
            tx.put('tasks',self.task)

    def issue(self,task=None):
        with self.store.tx() as tx:return policy.launch_issue(self.engine,tx,task or self.task)

    def test_valid_one_pdf_phase_and_canonical_record_required(self):
        self.assertIsNone(self.issue())
        (self.root/'projects/p/reports/technical.json').write_text('changed')
        self.assertIn('technical record',self.issue())
        task=dict(self.task,document_phase='technical_record',document_kind=None,role='engineer',model='opus',effort='high')
        self.assertIsNone(self.issue(task))

    def test_legacy_bulk_one_pdf_and_grant_caps(self):
        legacy=dict(self.task,work_kind=None,goal='Produce Q1 and Q2 paper_section and detailed_report PDFs',study_id=None)
        self.assertIn('legacy bulk',self.issue(legacy))
        self.assertIn('exactly one PDF',self.issue(dict(self.task,document_kind='both')))
        self.assertIn('model',self.issue(dict(self.task,model='opus')))
        large_output=copy.deepcopy(self.task);large_output['budget']['output_tokens']=60000
        self.assertIsNone(self.issue(large_output))
        extended=copy.deepcopy(self.task);extended['session_minutes']=120;extended['budget']['minutes']=720
        self.assertIsNone(self.issue(extended))
        task=copy.deepcopy(self.task);task['budget']['children']=1
        self.assertIn('children',self.issue(task))
        task=copy.deepcopy(self.task);task['budget']['sessions']=self.cfg['budgets']['task_sessions']
        self.assertIsNone(self.issue(task))

    def test_single_active_documentation_phase_across_roles(self):
        with self.store.tx() as tx:
            tx.put('tasks',dict(self.task,id='other',role='verifier',document_phase='check'))
            tx.put('sessions',dict(id='s',task='other',project='p',status='running'))
        self.assertIn('another documentation phase',self.issue())

    def test_prior_phase_sessions_do_not_impose_an_extra_documentation_cap(self):
        # Even stale legacy configuration must not reintroduce the special cap.
        self.cfg['documentation'].update(max_task_sessions=3,max_turns=30)
        with self.store.tx() as tx:
            tx.put('tasks',dict(self.task,id='old',status='cancelled'))
            for i in range(4):
                tx.put('sessions',dict(id=str(i),task='old',status='completed',seconds=60,usage={}))
        self.assertIsNone(self.issue())

    def test_unselected_study_cannot_preempt_current_writer(self):
        from labcore.runtime import Runtime
        from unittest.mock import patch
        with self.store.tx() as tx:
            tx.put('projects',dict(id='p',folder='p',status='active'))
            tx.put('channels',dict(id='C',active=True))
            tx.put('tasks',dict(self.task,status='running',priority=88))
            tx.put('tasks',dict(self.task,id='future',study_id='two',document_cycle=None,status='queued',priority=90))
            tx.put('sessions',dict(id='active',task='t',project='p',channel='C',role='literature',status='running',priority=88))
        runtime=SimpleNamespace(engine=self.engine)
        with patch('labcore.runtime.subscription.gate',return_value=None):
            Runtime.preempt(runtime)
        self.assertFalse(self.store.get('sessions','active').get('preempted',False))
        # An eligible, higher-priority task in the current study can still preempt.
        with self.store.tx() as tx:
            tx.put('tasks',dict(self.task,id='future',status='queued',priority=90))
        with patch('labcore.runtime.subscription.gate',return_value=None):
            Runtime.preempt(runtime)
        self.assertTrue(self.store.get('sessions','active')['preempted'])

    def test_no_next_study_or_new_cycle_before_publication(self):
        with self.store.tx() as tx:
            original=tx.get('documentation_focus','p')['cycle']
            policy.select_focus(tx,'p','one',root=self.root)
            self.assertEqual(original,tx.get('documentation_focus','p')['cycle'])
            for slug,new in [('two',False),('one',True)]:
                with self.assertRaisesRegex(ValueError,'Finish'):policy.select_focus(tx,'p',slug,new_cycle=new,root=self.root)
            study=tx.get('studies','p:one');study['documentation']='complete';tx.put('studies',study)
            task=dict(self.task,status='queued');tx.put('tasks',task)
            with self.assertRaisesRegex(ValueError,'open documentation'):policy.select_focus(tx,'p','two',root=self.root)
            task['status']='accepted';tx.put('tasks',task)
            policy.select_focus(tx,'p','two',root=self.root)
        self.assertIn('older or unselected',self.issue())

    def test_helpers_disabled_even_when_native_delegation_enabled(self):
        self.cfg['native_subagents']['enabled']=True
        session=dict(id='s',role='literature',work_kind='documentation',task='t')
        self.assertFalse(delegation.enabled(self.cfg,session))
        self.assertEqual(delegation.prepare(self.engine,session,{}),[])

    def test_defaults_and_job_bounds_integrate_with_platform(self):
        from tests.test_platform import PlatformTests
        fixture=PlatformTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.cfg['documentation']=self.cfg['documentation']
        project=fixture.channel['project']
        with fixture.engine.store.tx() as tx:
            tx.put('studies',dict(id=project+':one',study_id='one'))
            policy.select_focus(tx,project,'one')
        supervisor=dict(id='supervisor',project=project,channel='C1',role='supervisor')
        action=dict(goal='Write the paper-section PDF',acceptance='Readable checked PDF',work_kind='documentation',
                    study_id='one',document_phase='draft',document_kind='report',role='literature',model='sonnet',effort='medium')
        with fixture.engine.store.tx() as tx:task=fixture.engine.create_task(tx,supervisor,action)
        self.assertEqual(task['session_minutes'],fixture.cfg['scheduling']['max_session_minutes'])
        self.assertEqual(task['budget']['minutes'],fixture.cfg['budgets']['task_minutes'])
        self.assertEqual(task['budget']['sessions'],fixture.cfg['budgets']['task_sessions'])
        self.assertEqual(task['budget']['children'],0)
        self.assertEqual(task['resources']['max_gpus'],0)
        with fixture.engine.store.tx() as tx:
            self.assertIn('technical record',fixture.engine.task_launch_hold(tx,task)['reason'])
        session=fixture.session('literature',task)
        with fixture.engine.store.tx() as tx:
            session['status']='running';tx.put('sessions',session)
        for request in [dict(hours=.1,gpus=1),dict(hours=1,gpus=0)]:
            with self.assertRaisesRegex(ValueError,'Documentation jobs'):
                fixture.runtime.jobs.submit(session,request,'denied')

    def test_documentation_continuation_uses_saved_artifacts_and_focused_prompt(self):
        from tests.test_platform import PlatformTests
        from labcore import history
        fixture=PlatformTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        task=fixture.task(work_kind='documentation',role='literature',effort='medium')
        fixture.task(key='unrelated',goal='UNRELATED PROJECT BACKLOG')
        project=task['project']
        old=fixture.session('literature',task);old['status']='done'
        with fixture.engine.store.tx() as tx:
            tx.put('sessions',old)
            tx.put('project_notes',dict(id=project+':notes/research-story.md',project=project,
                path='notes/research-story.md',text='ENTIRE STORY AND HISTORICAL REVIEW '*10000))
        archive=history.project_directory(fixture.engine,project)/(old['id']+'.jsonl')
        archive.parent.mkdir(parents=True,exist_ok=True)
        archive.write_text(json.dumps(dict(type='user',message=dict(role='user',content='old conversation')))+'\n')
        fixture.cfg['documentation']=dict(self.cfg['documentation'],max_turns=30)
        fixture.cfg['claude']['max_turns']=150
        current=fixture.session('literature',task)
        work=Path(current['workspace']);(work/'control').mkdir(exist_ok=True)
        (work/'checkpoint.json').write_text(json.dumps(dict(completed=['paper draft saved'],next_step='Fix figure caption')))
        (work/'draft.tex').write_text('Existing draft, keep this content')
        with patch('labcore.runner.subprocess.Popen',return_value=Mock()) as launch:
            fixture.runtime.runner.start(current)
        command=launch.call_args.args[0];fixture.runtime.runner.processes.clear()
        self.assertNotIn('--resume',command)
        self.assertEqual(command[command.index('--max-turns')+1],'150')
        self.assertEqual(fixture.engine.store.get('sessions',current['id'])['history_mode'],'artifact_handoff')
        self.assertTrue(archive.is_file())
        self.assertEqual((work/'draft.tex').read_text(),'Existing draft, keep this content')
        user=json.loads(command[command.index('-p')+1])
        self.assertEqual(user['documentation_handoff']['mode'],'saved_artifacts')
        self.assertIn('Fix figure caption',json.dumps(user['checkpoint']))
        self.assertNotIn('ENTIRE STORY',json.dumps(user))
        self.assertNotIn('UNRELATED PROJECT BACKLOG',json.dumps(user))
        full=json.loads((work/'control/context.json').read_text())
        self.assertIn('ENTIRE STORY',full['research_story']['text'])
        self.assertEqual(user['task']['id'],task['id'])

    def test_handoff_does_not_change_research_history(self):
        from labcore import history
        session=dict(id='new',task='t',project='p',role='literature')
        self.assertEqual(history.prepare(self.engine,session),[])
        self.assertEqual(session['history_mode'],'artifact_handoff')
        with self.store.tx() as tx:tx.put('tasks',dict(id='t',project='p',goal='Research a new method',role='engineer'))
        session=dict(id='research',task='t',project='p',role='engineer')
        self.assertEqual(history.prepare(self.engine,session),[])
        self.assertNotIn('history_mode',session)
