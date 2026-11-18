"""Native delegation policy, interrupted accounting, and CLI integration checks."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_platform as fixtures
from labcore import delegation
from labcore.native_hook import handle, save_findings
from labcore.runner import parse_output


class NativeTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    cleanup_processes = fixtures.PlatformTests.cleanup_processes
    task = fixtures.PlatformTests.task
    session = fixtures.PlatformTests.session

    def prepare(self, **options):
        self.cfg['native_subagents'] = dict(dict(enabled=True, max_concurrent=2, default_model='sonnet'), **options)
        task = self.task(model='haiku', effort='low')
        session = self.session('engineer', task)
        session.update(model=task['model'], effort=task['effort'])
        with self.engine.store.tx() as tx:
            tx.put('sessions', session)
        env = self.runtime.runner.environment(session['workspace'])
        flags = delegation.prepare(self.engine, session, env)
        control = Path(session['workspace']) / 'control'
        return session, env, flags, control

    def event(self, ident='one', **args):
        return dict(hook_event_name='PreToolUse', tool_name='Agent', tool_use_id=ident,
                    tool_input=dict(subagent_type='lab-researcher', prompt='Check one source', **args))

    def test_cli_helpers_keep_profile_and_concurrency_limits_without_turn_caps(self):
        # Old configuration keys must not silently reintroduce the removed cap.
        session, env, flags, control = self.prepare(max_turns=12)
        self.assertEqual(env['CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH'], '1')
        self.assertEqual(env['CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'], '2')
        self.assertEqual(env['CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS'], '1')
        agents = json.loads(flags[flags.index('--agents') + 1])
        self.assertEqual(len(agents), 3)
        for agent in agents.values():
            self.assertEqual((agent['model'], agent['effort']), ('haiku', 'low'))
            self.assertNotIn('maxTurns', agent)
            self.assertIn('Agent', agent['disallowedTools'])
            self.assertNotIn('Agent', agent['tools'])
        self.assertNotIn('Bash', agents['lab-researcher']['tools'])

    def test_twelve_helpers_are_honored_and_thirteenth_waits(self):
        session,env,_,control=self.prepare(max_concurrent=12)
        self.assertEqual(env['CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'],'12')
        policy=json.loads((control/'native-policy.json').read_text())
        self.assertEqual(policy['max_concurrent'],12)
        state=dict(invocations={},agents={})
        for i in range(12):
            result=handle(self.event(str(i)),policy,state)['hookSpecificOutput']
            self.assertEqual(result['permissionDecision'],'allow')
            handle(dict(hook_event_name='SubagentStart',agent_id=str(i),agent_type='lab-researcher'),policy,state)
        event=self.event('thirteenth')
        self.assertEqual(handle(event,policy,state)['hookSpecificOutput']['permissionDecision'],'deny')
        handle(dict(hook_event_name='SubagentStop',agent_id='0',agent_type='lab-researcher'),policy,state)
        self.assertEqual(handle(event,policy,state)['hookSpecificOutput']['permissionDecision'],'allow')

    def test_invalid_helper_limits_are_rejected_without_silent_coercion(self):
        for value in (0,-1,True,2.5,'12',None):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'positive integer'):
                delegation.concurrency_limit(dict(max_concurrent=value))
        self.assertEqual(delegation.concurrency_limit(dict(max_concurrent=24)),24)

    def test_helper_history_survives_restarts_without_a_launch_allowance(self):
        session, env, flags, control = self.prepare()
        hook = self.root / 'labcore/native_hook.py'
        def invoke(event):
            result = subprocess.run([sys.executable, str(hook), str(control)], input=json.dumps(event), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout).get('hookSpecificOutput', {}).get('permissionDecision')
        self.assertEqual(invoke(self.event()), 'allow')
        self.assertEqual(invoke(self.event()), 'allow')
        delegation.prepare(self.engine, session, env)
        invoke(dict(hook_event_name='SubagentStart', agent_id='one', agent_type='lab-researcher'))
        invoke(dict(hook_event_name='SubagentStop', agent_id='one', agent_type='lab-researcher'))
        # Exceed the old assignment.children=2 and default four-launch cap.
        for i in range(12):
            self.assertEqual(invoke(self.event(str(i))), 'allow')
            invoke(dict(hook_event_name='SubagentStart', agent_id=str(i), agent_type='lab-researcher'))
            invoke(dict(hook_event_name='SubagentStop', agent_id=str(i), agent_type='lab-researcher'))
        self.assertEqual(len(json.loads((control/'native-state.json').read_text())['invocations']), 13)

    def test_upgrades_unapproved_agent_types_and_foreign_resumes_are_denied(self):
        _, _, _, control = self.prepare()
        policy = json.loads((control/'native-policy.json').read_text())
        state = dict(invocations={}, agents={})
        for event in (self.event(model='opus'), self.event(effort='high'), self.event(resume='foreign'),
                      dict(self.event(), tool_input={'subagent_type': 'general-purpose'})):
            self.assertEqual(handle(event, policy, state)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertFalse(state['invocations'])

    def test_native_reports_are_saved_and_followups_have_no_lifetime_cap(self):
        _, _, _, control = self.prepare()
        policy = json.loads((control/'native-policy.json').read_text())
        state = dict(invocations={}, agents={})
        handle(self.event(), policy, state)
        handle(dict(hook_event_name='SubagentStart', agent_id='a', agent_type='lab-researcher'), policy, state)
        handle(dict(hook_event_name='SubagentStop', agent_id='a', agent_type='lab-researcher',
                    last_assistant_message='Source says 4; method unclear', agent_transcript_path='/local/transcript'), policy, state)
        self.assertEqual(state['agents']['a']['status'], 'done')
        self.assertIn('method unclear', state['agents']['a']['summary'])
        event = dict(hook_event_name='PreToolUse', tool_name='SendMessage', tool_use_id='followup', tool_input={'to': 'a', 'message': 'Clarify'})
        self.assertEqual(handle(event, policy, state)['hookSpecificOutput']['permissionDecision'], 'allow')
        for i in range(12):
            handle(dict(hook_event_name='SubagentStart', agent_id='a', agent_type='lab-researcher'), policy, state)
            handle(dict(hook_event_name='SubagentStop', agent_id='a', agent_type='lab-researcher'), policy, state)
            self.assertEqual(handle(dict(event, tool_use_id='again-'+str(i)), policy, state)['hookSpecificOutput']['permissionDecision'], 'allow')

    def test_complete_findings_are_saved_verbatim_and_versions_survive_resume(self):
        session,_,_,control=self.prepare();policy=json.loads((control/'native-policy.json').read_text());workspace=control.parent
        state=dict(invocations={},agents={});handle(self.event(),policy,state)
        start=handle(dict(hook_event_name='SubagentStart',agent_id='reader',agent_type='lab-researcher'),policy,state,workspace)
        self.assertIn('helpers/native-results/reader/findings.md',start['hookSpecificOutput']['additionalContext'])
        text='# Evidence\n'+('Full Unicode findings θ with source links.\n'*400)
        stop=dict(hook_event_name='SubagentStop',agent_id='reader',agent_type='lab-researcher',last_assistant_message=text)
        self.assertEqual(handle(stop,policy,state,workspace),{})
        agent=state['agents']['reader'];self.assertEqual((workspace/agent['report']).read_text(),text)
        self.assertGreater(agent['report_bytes'],6000);self.assertEqual(len(agent['report_versions']),1)
        notice=handle(dict(hook_event_name='PostToolUse',tool_name='Agent'),policy,state,workspace)
        self.assertIn(agent['report'],notice['hookSpecificOutput']['additionalContext'])
        self.assertEqual(handle(dict(hook_event_name='PostToolUse',tool_name='Agent'),policy,state,workspace),{})
        handle(stop,policy,state,workspace);self.assertEqual(len(agent['report_versions']),1)
        handle(dict(stop,last_assistant_message='Follow-up with corrected evidence'),policy,state,workspace)
        self.assertEqual(len(agent['report_versions']),2)
        self.assertEqual((workspace/agent['report_versions'][0]).read_text(),text)
        handle(dict(stop,last_assistant_message=''),policy,state,workspace)
        self.assertEqual((workspace/agent['report']).read_text(),'Follow-up with corrected evidence')
        index=json.loads((workspace/'helpers/native-results/index.json').read_text())
        self.assertEqual(index['reports'][0]['agent_id'],'reader')
        self.assertNotIn('transcript',index['reports'][0])

    def test_auto_save_refuses_symlink_directories_and_does_not_restart_helper(self):
        _,_,_,control=self.prepare();policy=json.loads((control/'native-policy.json').read_text());workspace=control.parent
        outside=self.root/'outside';outside.mkdir();(workspace/'helpers').symlink_to(outside,target_is_directory=True)
        state=dict(invocations={},agents={})
        result=handle(dict(hook_event_name='SubagentStop',agent_id='reader',agent_type='lab-researcher',last_assistant_message='Preserved in transcript'),policy,state,workspace)
        self.assertEqual(result,{})
        self.assertIn('report_error',state['agents']['reader']);self.assertEqual(list(outside.iterdir()),[])
        self.assertEqual(state['agents']['reader']['status'],'done')
        notice=handle(dict(hook_event_name='PostToolUse',tool_name='Agent'),policy,state,workspace)
        self.assertIn('without repeating the research',notice['hookSpecificOutput']['additionalContext'])

    def test_auto_save_rejects_unsafe_ids_and_replaces_leaf_symlinks_without_following(self):
        _,_,_,control=self.prepare();workspace=control.parent;agent={}
        with self.assertRaises(ValueError):save_findings(workspace,'../../escape','bad',agent)
        target=self.root/'protected';target.write_text('Original')
        folder=workspace/'helpers/native-results/reader';folder.mkdir(parents=True)
        (folder/'findings.md').symlink_to(target)
        save_findings(workspace,'reader','New findings',agent)
        self.assertEqual(target.read_text(),'Original');self.assertFalse((folder/'findings.md').is_symlink())
        self.assertEqual((folder/'findings.md').read_text(),'New findings')

    def test_hook_process_saves_text_without_an_extra_stop_turn(self):
        _,_,_,control=self.prepare()
        event=dict(hook_event_name='SubagentStop',agent_id='reader',agent_type='lab-researcher',last_assistant_message='Full final findings')
        result=subprocess.run([sys.executable,str(self.root/'labcore/native_hook.py'),str(control)],input=json.dumps(event),text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout),{})
        self.assertEqual((control.parent/'helpers/native-results/reader/findings.md').read_text(),'Full final findings')

    def test_specialist_can_choose_helper_turn_limit(self):
        _, _, _, control = self.prepare()
        policy = json.loads((control/'native-policy.json').read_text())
        policy.update(max_turns=12, max_invocations=0) # stale fields are not enforced
        state = dict(invocations={}, agents={})
        result = handle(self.event(max_turns=50), policy, state)['hookSpecificOutput']
        self.assertEqual(result['permissionDecision'], 'allow')
        self.assertEqual(result['updatedInput']['max_turns'], 50)

    def test_supervisor_verifier_and_legacy_workers_cannot_delegate(self):
        session, env, _, _ = self.prepare()
        for role in ('supervisor', 'verifier', 'worker'):
            session['role'] = role
            self.assertEqual(delegation.prepare(self.engine, session, env), [])

    def test_final_aggregate_and_forwarded_helpers_are_not_double_charged(self):
        def message(ident, count, parent=None):
            return dict(type='assistant', parent_tool_use_id=parent, message=dict(id=ident, usage=dict(output_tokens=count)))
        records = [message('main', 5), message('main', 8), message('child', 7, 'tool1'),
                   dict(type='result', usage={'output_tokens': 8, 'service_tier': 'standard'},
                        modelUsage={'haiku': {'outputTokens': 15, 'inputTokens': 30}})]
        _, usage = parse_output('\n'.join(map(json.dumps, records)))
        self.assertEqual(usage['output_tokens'], 15)
        self.assertEqual(usage['input_tokens'], 30)
        self.assertNotIn('service_tier', usage)
        _, partial = parse_output('\n'.join(map(json.dumps, records[:-1])) + '\n{"type":')
        self.assertEqual(partial['output_tokens'], 15)

    def test_resume_charges_only_incremental_model_usage_including_helpers(self):
        data=json.dumps(dict(type='result',usage={'output_tokens':50},
            modelUsage={'opus':{'outputTokens':1200,'inputTokens':3000}}))
        _,usage=parse_output(data,resumed=True,model_usage_baseline={'opus':{'outputTokens':1000,'inputTokens':2000}})
        self.assertEqual(usage['output_tokens'],200)
        self.assertEqual(usage['input_tokens'],1000)
        _,unknown=parse_output(data,resumed=True)
        self.assertEqual(unknown['output_tokens'],50)

    def test_empty_helper_tail_cannot_hide_quota_or_partial_report(self):
        partial={'status':'partial','summary':'PDFs built; checks remain'}
        records=[dict(type='result',is_error=False,structured_output=partial,usage={'output_tokens':80}),
                 dict(type='result',is_error=True,result="You've hit your session limit",usage={'output_tokens':0}),
                 dict(type='result',is_error=False,num_turns=0,result='',usage={'output_tokens':0})]
        final,usage=parse_output('\n'.join(map(json.dumps,records)))
        self.assertTrue(final['is_error'])
        self.assertIn('session limit',final['result'])
        self.assertEqual(final['saved_partial_output'],partial)
        self.assertEqual(usage['output_tokens'],80)
        final,_=parse_output('\n'.join(map(json.dumps,[records[0],records[-1]])))
        self.assertEqual(final['structured_output'],partial)

    def test_missing_policy_fails_closed_before_spawning(self):
        _, _, _, control = self.prepare()
        (control/'native-policy.json').unlink()
        result = subprocess.run([sys.executable, str(self.root/'labcore/native_hook.py'), str(control)],
                                input=json.dumps(self.event()), text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_concurrent_followups_cannot_bypass_native_spawn_limit(self):
        _, _, _, control = self.prepare()
        policy = json.loads((control/'native-policy.json').read_text())
        policy.update(max_concurrent=1)
        state = dict(invocations={}, agents={})
        for aid in ('a', 'b'):
            state['agents'][aid] = dict(status='done', session=policy['session'])
        event = dict(hook_event_name='PreToolUse', tool_name='SendMessage', tool_use_id='resume-a', tool_input={'to': 'a', 'message': 'Continue'})
        self.assertEqual(handle(event, policy, state)['hookSpecificOutput']['permissionDecision'], 'allow')
        # SubagentStart has not arrived yet; the pending reservation still counts.
        event.update(tool_use_id='resume-b', tool_input={'to': 'b', 'message': 'Continue'})
        self.assertEqual(handle(event, policy, state)['hookSpecificOutput']['permissionDecision'], 'deny')
        handle(dict(hook_event_name='SubagentStart', agent_id='a', agent_type='lab-researcher'), policy, state)
        self.assertEqual(handle(event, policy, state)['hookSpecificOutput']['permissionDecision'], 'deny')
        handle(dict(hook_event_name='SubagentStop', agent_id='a', agent_type='lab-researcher'), policy, state)
        self.assertEqual(handle(event, policy, state)['hookSpecificOutput']['permissionDecision'], 'allow')

    def test_new_parent_session_retains_history_and_marks_orphaned_helper(self):
        session, env, _, control = self.prepare()
        state = dict(invocations={'used': {'session': session['id'], 'status': 'reserved'}},
                     agents={'a': {'status': 'running', 'session': session['id']}})
        (control/'native-state.json').write_text(json.dumps(state))
        session['id'] = 'new-parent'
        delegation.prepare(self.engine, session, env)
        restored = json.loads((control/'native-state.json').read_text())
        self.assertEqual(restored['agents']['a']['status'], 'interrupted')
        self.assertIn('used', restored['invocations'])

    def test_native_assignment_cannot_also_spawn_managed_workers(self):
        session, _, _, _ = self.prepare()
        task = self.engine.store.get('tasks', session['task'])
        with self.engine.store.tx() as tx:
            with self.assertRaisesRegex(ValueError, 'native CLI helpers'):
                self.engine._action(tx, session, task, dict(type='spawn'))

    def test_launch_exposes_native_tools_only_to_specialist_and_keeps_sandbox(self):
        session, _, _, _ = self.prepare()
        with patch('labcore.runner.subprocess.Popen') as launch:
            self.runtime.runner.start(session)
        args = launch.call_args.args[0]
        env = launch.call_args.kwargs['env']
        self.assertIn('sandbox.py', ' '.join(args))
        self.assertIn('Agent', args[args.index('--tools') + 1])
        self.assertIn('--forward-subagent-text', args)
        self.assertEqual(env['CUDA_VISIBLE_DEVICES'], '')
        # The mocked object is not a live process to clean up.
        self.runtime.runner.processes.clear()
