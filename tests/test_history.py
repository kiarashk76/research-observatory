"""Conversation archives and forks stay independent of account credentials."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_platform as fixtures
from labcore import history


class HistoryTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    cleanup_processes = fixtures.PlatformTests.cleanup_processes
    session = fixtures.PlatformTests.session
    task = fixtures.PlatformTests.task

    def previous(self, role='supervisor', task=None, account=None):
        old = self.session(role, task)
        old.update(status='done', account=account)
        with self.engine.store.tx() as tx:
            tx.put('sessions', old)
        root = self.root/'runtime/claude-auth/config'
        if account:
            self.cfg['claude']['accounts'] = [dict(id=account, config_dir='runtime/claude-auth/'+account+'/config')]
            root = self.root/'runtime/claude-auth'/account/'config'
        source = root/'projects/workspace'/(old['id']+'.jsonl')
        source.parent.mkdir(parents=True,exist_ok=True)
        source.write_text(json.dumps(dict(type='user',sessionId=old['id'],message=dict(role='user',content='previous work'))) + '\n')
        return old, source

    def test_cross_account_forks_immutable_archive(self):
        old, source = self.previous(account='primary')
        new = self.session()
        new['account'] = 'secondary'
        flags = history.prepare(self.engine,new)
        self.assertIn('--fork-session',flags)
        self.assertEqual(new['history_parent'],old['id'])
        archive = Path(new['history_transcript'])
        self.assertEqual(archive.read_bytes(),source.read_bytes())
        self.assertTrue(archive.is_relative_to(self.root/'runtime/claude-history'))
        source.write_text('changed')
        self.assertEqual(history.archive(self.engine,old).read_text(),archive.read_text())
        self.assertNotEqual(archive.read_text(),source.read_text())

    def test_writing_reset_starts_supervisor_fresh_preserving_research_history(self):
        old,source=self.previous()
        engineering=self.task(key='ongoing-experiment')
        engineer,_=self.previous('engineer',engineering)
        with self.engine.store.tx() as tx:
            project=tx.get('projects',old['project']);project['writing_reset_at']=max(old['created'],engineer['created'])+1
            tx.put('projects',project)
        self.assertEqual(history.prepare(self.engine,self.session()),[])
        self.assertTrue(source.is_file())
        self.assertIn('--resume',history.prepare(self.engine,self.session('engineer',engineering)))

    def test_no_cross_thread_role_project_or_assignment_resume(self):
        old,_ = self.previous()
        for key,value in [('thread','other'),('channel','other'),('role','engineer'),('project','other')]:
            new = self.session()
            new[key] = value
            self.assertEqual(history.prepare(self.engine,new),[])
        task1=self.task(key='first')
        self.previous('engineer',task1)
        task2=self.task(key='second')
        self.assertEqual(history.prepare(self.engine,self.session('engineer',task2)),[])
        self.assertIn('--resume',history.prepare(self.engine,self.session('engineer',task1)))

    def test_running_sessions_and_symlinks_are_not_archived(self):
        old, source=self.previous()
        old['status']='running'
        self.assertIsNone(history.archive(self.engine,old))
        old['status']='done'
        outside=self.root/'private.jsonl'
        outside.write_text('private')
        source.unlink();source.symlink_to(outside)
        self.assertIsNone(history.archive(self.engine,old))

    def test_latest_available_transcript_and_opt_out(self):
        old,_=self.previous()
        newer=self.session()
        newer['status']='done'
        with self.engine.store.tx() as tx:tx.put('sessions',newer)
        new=self.session()
        history.prepare(self.engine,new)
        self.assertEqual(new['history_parent'],old['id'])
        self.cfg['claude']['shared_session_history']=False
        self.assertEqual(history.prepare(self.engine,self.session()),[])

    def test_history_is_readable_but_not_writable_in_sandbox(self):
        old,_=self.previous()
        current=self.session()
        history.prepare(self.engine,current)
        archive=Path(current['history_transcript'])
        workspace=Path(current['workspace']);workspace.mkdir(parents=True,exist_ok=True)
        env=self.runtime.runner.environment(workspace)
        code='from pathlib import Path; p=Path('+repr(str(archive))+'); print(p.read_text()); p.write_text("overwrite")'
        command=self.runtime.runner.sandbox_command([sys.executable,'-c',code],current,env)
        result=subprocess.run(command,env=env,capture_output=True,text=True)
        self.assertIn('previous work',result.stdout)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('PermissionError',result.stderr)

    def test_runner_launch_preserves_current_controls_and_records_lineage(self):
        old,_=self.previous()
        current=self.session()
        with patch('labcore.runner.subprocess.Popen',return_value=Mock()) as launch:
            self.runtime.runner.start(current)
        command=launch.call_args.args[0]
        self.assertIn('--resume',command)
        self.assertIn('--fork-session',command)
        self.assertEqual(command[command.index('--session-id')+1],current['id'])
        self.assertEqual(command[command.index('--permission-mode')+1],'acceptEdits')
        self.assertEqual(command[command.index('--system-prompt-snapshot')+1],'off')
        saved=self.engine.store.get('sessions',current['id'])
        self.assertEqual(saved['history_parent'],old['id'])
        self.assertTrue(Path(saved['history_transcript']).is_file())
        self.runtime.runner.processes.clear()

    def test_empty_or_malformed_conversations_fall_back_to_lab_context(self):
        old,source=self.previous()
        for content in ('{"type":"system"}\n', '{"type":', ''):
            source.write_text(content)
            self.assertEqual(history.prepare(self.engine,self.session()),[])
