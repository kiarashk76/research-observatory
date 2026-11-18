"""Small deterministic CLI hook. No models, credentials, or platform DB access."""
import fcntl
import hashlib
import os
import re
import uuid
import json
import sys
import time
from pathlib import Path

MODELS = ('haiku', 'sonnet', 'opus')
EFFORTS = ('low', 'medium', 'high', 'xhigh')
TYPES = {'lab-researcher', 'lab-implementer', 'lab-reviewer'}


REPORT_ROOT = 'helpers/native-results'


def report_path(agent_id):
    if not isinstance(agent_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',agent_id):
        raise ValueError('Invalid helper identity for report storage')
    return f'{REPORT_ROOT}/{agent_id}/findings.md'


def write_workspace_file(workspace, relative, data):
    """Atomic output beneath the workspace, without following directory symlinks."""
    parts=relative.split('/')
    if any(part in ('','.','..') for part in parts):raise ValueError('Invalid helper output path')
    descriptor=os.open(workspace,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    temporary=None
    try:
        for part in parts[:-1]:
            try:os.mkdir(part,0o700,dir_fd=descriptor)
            except FileExistsError:pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor);descriptor=child
        temporary='.'+parts[-1]+'.'+uuid.uuid4().hex+'.tmp'
        output=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=descriptor)
        with os.fdopen(output,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,parts[-1],src_dir_fd=descriptor,dst_dir_fd=descriptor)
        temporary=None
    finally:
        if temporary:
            try:os.unlink(temporary,dir_fd=descriptor)
            except FileNotFoundError:pass
        os.close(descriptor)


def save_findings(workspace, agent_id, text, agent):
    if not isinstance(text,str) or not text.strip():return
    relative=report_path(agent_id);data=text.encode('utf-8');digest=hashlib.sha256(data).hexdigest()
    version=relative.rsplit('/',1)[0]+'/responses/'+digest+'.md'
    write_workspace_file(workspace,version,data)
    write_workspace_file(workspace,relative,data)
    agent.update(report=relative,report_sha256=digest,report_bytes=len(data),report_updated=time.time())
    agent.pop('report_error',None)
    versions=agent.setdefault('report_versions',[])
    if version not in versions:versions.append(version)


def save_report_index(workspace,state):
    reports=[dict(agent_id=aid,**{key:agent[key] for key in ('session','type','status','report','report_sha256','report_bytes','report_updated','report_versions','report_error') if key in agent})
             for aid,agent in state['agents'].items() if agent.get('report') or agent.get('report_error')]
    write_workspace_file(workspace,REPORT_ROOT+'/index.json',json.dumps(dict(reports=reports),indent=2).encode())


def decision(allowed, reason, updated=None):
    output = {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow' if allowed else 'deny',
              'permissionDecisionReason': reason}
    if updated is not None:
        output['updatedInput'] = updated
    return {'hookSpecificOutput': output}


def handle(event, policy, state, workspace=None):
    kind = event.get('hook_event_name')
    if kind in ('SubagentStart', 'SubagentStop'):
        aid = event.get('agent_id')
        if not aid or event.get('agent_type') not in TYPES:
            return {}
        agent = state['agents'].setdefault(aid, {})
        agent.update(type=event['agent_type'], session=policy['session'],
                     status='running' if kind == 'SubagentStart' else 'done', updated=time.time())
        if kind == 'SubagentStart':
            pending = [v for v in state['invocations'].values() if v.get('session') == policy['session'] and v.get('status') == 'reserved']
            if pending:
                pending[0].update(status='started', agent=aid)
        if kind == 'SubagentStop':
            text=event.get('last_assistant_message','')
            agent['summary'] = str(text)[-6000:]
            agent['transcript'] = event.get('agent_transcript_path')
            if workspace is not None:
                try:
                    save_findings(workspace,aid,text,agent)
                    if agent.get('report'):save_report_index(workspace,state)
                except (OSError,ValueError) as error:
                    agent['report_error']='Automatic findings save failed: '+str(error)
            # A Stop reminder would make the helper continue, so never return one.
            return {}
        try:relative=report_path(aid)
        except ValueError:return {}
        return {'hookSpecificOutput':{'hookEventName':'SubagentStart','additionalContext':
                'Your final response will be saved verbatim by the platform as '+relative+'. Return your complete findings as final-response text; do not write that report file yourself. Code, tests and paper-note JSONs explicitly assigned by the specialist may still be written normally.'}}
    if kind == 'PostToolUse':
        notices=[]
        for aid,agent in state['agents'].items():
            if agent.get('session')!=policy['session']:continue
            if agent.get('report_error') and agent.get('announced_error')!=agent['report_error']:
                notices.append(aid+': '+agent['report_error']+'. The returned text is still available; save it in the parent report without repeating the research.')
                agent['announced_error']=agent['report_error']
            elif agent.get('report_sha256') and agent.get('announced_report')!=agent['report_sha256']:
                notices.append(aid+': '+agent['report']+' (complete final response; prior responses retained in responses/).')
                agent['announced_report']=agent['report_sha256']
        if notices:return {'hookSpecificOutput':{'hookEventName':'PostToolUse','additionalContext':'Platform-saved helper findings:\n'+'\n'.join(notices)}}
        return {}
    if kind != 'PreToolUse':
        return {}
    tool = event.get('tool_name')
    args = dict(event.get('tool_input') or {})
    target = args.get('to') or args.get('resume')
    known = state['agents'].get(target, {})
    if tool == 'SendMessage':
        if not known or known.get('session') != policy['session']:
            return decision(False, 'Only communicate with a helper in this specialist session. Read saved reports for other assignments.')
    elif tool in ('Agent', 'Task'):
        if args.get('subagent_type') not in TYPES:
            return decision(False, 'Use lab-researcher, lab-implementer, or lab-reviewer within this assignment.')
    else:
        return decision(False, 'Unsupported delegation tool')
    model = args.get('model', policy['default_model'])
    effort = args.get('effort', policy['default_effort'])
    effort = {'extra': 'xhigh'}.get(effort, effort)
    if model not in MODELS or MODELS.index(model) > MODELS.index(policy['model']):
        return decision(False, 'Helper model exceeds the supervisor grant; request an escalation.')
    if effort not in EFFORTS or EFFORTS.index(effort) > EFFORTS.index(policy['effort']):
        return decision(False, 'Helper effort exceeds the supervisor grant; request an escalation.')
    if target and not known:
        return decision(False, 'Unknown helper; use its saved artifacts instead of resuming another assignment.')
    ident = str(event.get('tool_use_id') or '')
    if not ident:
        return decision(False, 'Missing delegation invocation identity')
    # Record launches/follow-ups for recovery, without imposing a lifetime count.
    # Time, token accounting and experiment resources belong to the assignment.
    if ident not in state['invocations']:
        active = sum(a.get('status') == 'running' and a.get('session') == policy['session'] for a in state['agents'].values())
        pending = sum(v.get('session') == policy['session'] and v.get('status') == 'reserved' for v in state['invocations'].values())
        new_slot = known.get('status') != 'running'
        if new_slot and active + pending >= policy['max_concurrent']:
            return decision(False, 'Wait for an active helper to finish before resuming another.')
        state['invocations'][ident] = {'session': policy['session'], 'tool': tool, 'model': model,
                                      'effort': effort, 'created': time.time(), 'status': 'reserved' if new_slot else 'message'}
    # Explicit profiles prevent an omitted value inheriting an unexpected definition.
    args.update(model=model, effort=effort)
    return decision(True, 'Bounded helper authorized within the specialist assignment.', args)


def main():
    try:
        control = Path(sys.argv[1])
        event = json.load(sys.stdin)
        policy = json.loads((control / 'native-policy.json').read_text())
        with (control / 'native-state.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = control / 'native-state.json'
            state = json.loads(path.read_text())
            result = handle(event, policy, state, workspace=control.resolve().parent)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(state, indent=2))
            temp.replace(path)
        print(json.dumps(result))
    except Exception:
        # Fail closed on launches; do not turn a report-save error into a Stop loop.
        if 'event' in locals() and event.get('hook_event_name')!='PreToolUse':
            print('Native helper event could not be recorded; the transcript retains its response.',file=sys.stderr)
            print('{}');return
        print('Native delegation policy could not be checked; save progress and report the blocker.', file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
