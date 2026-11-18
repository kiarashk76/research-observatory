"""Native Claude subagent configuration inside a bounded specialist assignment."""
import json
import shlex
import sys
from pathlib import Path

from .files import atomic_json

TOOLS = 'Read,Write,Edit,Glob,Grep,Bash,WebSearch,WebFetch'
NATIVE_TOOLS = 'Agent,SendMessage,TaskOutput,TaskStop'
MODELS = ('haiku', 'sonnet', 'opus')
EFFORTS = ('low', 'medium', 'high', 'xhigh')


def concurrency_limit(settings):
    value = settings.get('max_concurrent', 2)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('native_subagents.max_concurrent must be a positive integer')
    return value


def enabled(cfg, session):
    if session.get('work_kind') == 'documentation' and cfg.get('documentation', {}).get('enabled', False):
        return False
    return cfg.get('native_subagents', {}).get('enabled', True) and session['role'] in ('engineer', 'literature')


def prepare(engine, session, env):
    """Return extra CLI flags; retain helper history across specialist retries."""
    if not enabled(engine.cfg, session):
        return []
    task = engine.store.get('tasks', session.get('task', ''))
    if not task:
        return []
    from .documentation_policy import is_documentation
    if engine.cfg.get('documentation', {}).get('enabled', False) and is_documentation(task):
        return []
    cfg = engine.cfg.get('native_subagents', {})
    concurrent = concurrency_limit(cfg)
    default_model = cfg.get('default_model', 'sonnet')
    if default_model == 'inherit':
        default_model = session['model']
    if default_model not in MODELS:
        raise ValueError('native_subagents.default_model must be haiku, sonnet, opus, or inherit')
    model = MODELS[min(MODELS.index(default_model), MODELS.index(session['model']))]
    effort = EFFORTS[min(EFFORTS.index(session['effort']), 1)]
    workspace = Path(session['workspace'])
    control = workspace / 'control'
    control.mkdir(parents=True, exist_ok=True)
    policy = dict(session=session['id'], model=session['model'], effort=session['effort'],
                  default_model=model, default_effort=effort, max_concurrent=concurrent)
    atomic_json(control / 'native-policy.json', policy)
    state_file = control / 'native-state.json'
    state = json.loads(state_file.read_text()) if state_file.exists() else {'invocations': {}, 'agents': {}}
    # A new controller session never treats children from an interrupted CLI as live.
    for agent in state['agents'].values():
        if agent.get('status') == 'running':
            agent['status'] = 'interrupted'
    atomic_json(state_file, state)
    env.update(CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=str(concurrent), CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH='1',
               CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS='1')
    prompt = (engine.root / 'prompts/native_worker.md').read_text()
    agents = {}
    for name, description, tools in (
        ('lab-researcher', 'Investigate a bounded source, paper, or code question; return evidence and uncertainty.', 'Read,Glob,Grep,WebSearch,WebFetch'),
        ('lab-implementer', 'Implement or debug one assigned component in explicitly assigned files; run focused tests.', TOOLS),
        ('lab-reviewer', 'Read code and evidence critically and identify errors; this is an internal review, not independent acceptance.', 'Read,Glob,Grep,Bash,WebSearch,WebFetch'),
    ):
        agents[name] = dict(description=description, prompt=prompt, tools=tools.split(','),
                            disallowedTools=['Agent', 'Task', 'SendMessage'], model=model, effort=effort,
                            permissionMode='acceptEdits')
    hook = shlex.join([sys.executable, str(engine.root / 'labcore/native_hook.py'), str(control)])
    settings = {'hooks': {
        'PreToolUse': [{'matcher': 'Agent|Task|SendMessage', 'hooks': [{'type': 'command', 'command': hook, 'timeout': 5}]}],
        'PostToolUse': [{'matcher': 'Agent|Task|TaskOutput|SendMessage', 'hooks': [{'type': 'command', 'command': hook, 'timeout': 5}]}],
        'SubagentStart': [{'matcher': 'lab-.*', 'hooks': [{'type': 'command', 'command': hook, 'timeout': 5}]}],
        'SubagentStop': [{'matcher': 'lab-.*', 'hooks': [{'type': 'command', 'command': hook, 'timeout': 5}]}],
    }}
    return ['--agents', json.dumps(agents), '--settings', json.dumps(settings), '--forward-subagent-text']
