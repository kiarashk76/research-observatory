"""Shared memory and plugin configuration retain per-account authentication."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_platform as fixtures
from labcore import customization, delegation


class CustomizationTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    cleanup_processes = fixtures.PlatformTests.cleanup_processes
    session = fixtures.PlatformTests.session
    task = fixtures.PlatformTests.task

    def test_both_accounts_use_same_memory_and_preserve_native_hooks(self):
        task=self.task()
        session=self.session('engineer',task)
        self.cfg['native_subagents']['enabled']=True
        flags=delegation.prepare(self.engine,session,{})
        before=json.loads(flags[flags.index('--settings')+1])
        shared=customization.prepare(self.engine,session,flags)
        settings=json.loads(shared[shared.index('--settings')+1])
        self.assertEqual(settings['hooks'],before['hooks'])
        other=dict(session,account='secondary')
        flags2=customization.prepare(self.engine,other,[])
        self.assertEqual(settings['autoMemoryDirectory'],json.loads(flags2[flags2.index('--settings')+1])['autoMemoryDirectory'])

    def test_shared_plugins_load_identically_and_stay_read_only(self):
        package=self.root/'runtime/claude-shared/plugins/example'
        (package/'.claude-plugin').mkdir(parents=True)
        (package/'.claude-plugin/plugin.json').write_text('{"name":"example"}')
        self.cfg['claude']['shared_plugin_dirs']=['runtime/claude-shared/plugins']
        current=self.session()
        with patch('labcore.runner.subprocess.Popen',return_value=Mock()) as launch:
            self.runtime.runner.start(current)
        command=launch.call_args.args[0]
        self.assertEqual(command[command.index('--plugin-dir')+1],str(package))
        self.assertNotIn('--disable-slash-commands',command)
        self.assertIn('Skill',command[command.index('--tools')+1].split(','))
        self.assertIn('Skill',command[command.index('--allowedTools')+1].split(','))
        self.assertIn('--strict-mcp-config',command)
        self.assertEqual(command[command.index('--max-turns')+1],str(self.cfg['claude']['max_turns']))
        self.assertEqual(command[command.index('--disallowedTools')+1],'Agent,Task,SendMessage')
        self.assertIn(str(package),current['shared_plugin_paths'])
        self.runtime.runner.processes.clear()

    def test_memory_writable_only_for_own_project(self):
        session=self.session()
        shared=customization.memory_directory(self.engine,session['project'])
        foreign=customization.memory_directory(self.engine,'other')
        shared.mkdir(parents=True);foreign.mkdir(parents=True)
        workspace=Path(session['workspace']);workspace.mkdir(parents=True,exist_ok=True)
        env=self.runtime.runner.environment(workspace)
        script='from pathlib import Path; Path('+repr(str(shared/'note.md'))+').write_text("learned"); Path('+repr(str(foreign/'note.md'))+').write_text("foreign")'
        command=self.runtime.runner.sandbox_command([sys.executable,'-c',script],session,env)
        result=subprocess.run(command,env=env,capture_output=True,text=True)
        self.assertEqual((shared/'note.md').read_text(),'learned')
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((foreign/'note.md').exists())

    def test_import_is_idempotent_and_preserves_original_links(self):
        session=self.session()
        import re
        encoded=re.sub(r'[^A-Za-z0-9]','-',str(Path(session['workspace']).resolve()))
        memory=self.root/'runtime/claude-auth/config/projects'/encoded/'memory'
        memory.mkdir(parents=True)
        (memory/'MEMORY.md').write_text('[Lesson](lesson.md)')
        (memory/'lesson.md').write_text('A useful lesson')
        self.assertEqual(customization.import_memory(self.engine),2)
        self.assertEqual(customization.import_memory(self.engine),0)
        shared=customization.memory_directory(self.engine,session['project'])
        imported=list((shared/'imported').glob('*/MEMORY.md'))[0]
        self.assertTrue((imported.parent/'lesson.md').exists())
        self.assertEqual((memory/'MEMORY.md').read_text(),'[Lesson](lesson.md)')
