"""Opt-in: one tiny specialist + one native helper, no Slack messages or experiments."""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labcore.config import ROOT, load
from labcore import accounts
from labcore.engine import Engine
from labcore.runtime import Runtime


def main():
    with tempfile.TemporaryDirectory(prefix='research-native-check-') as folder:
        root = Path(folder)
        for name in ('labcore', 'prompts'):
            shutil.copytree(ROOT/name, root/name, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROOT/'lab.py', root/'lab.py')
        cfg = load()
        for account in cfg['claude'].get('accounts',[]):account['config_dir']=str(accounts.directory(cfg,account['id']))
        cfg['claude']['shared_memory']=False
        cfg['root'] = str(root)
        cfg['subscription'] = {'enabled': False}
        cfg['claude'].update(api_retries=0, api_timeout_ms=25000, debug=True)
        cfg['scheduling']['debounce_seconds'] = 0
        engine = Engine(cfg)
        runtime = Runtime(engine)
        channel = engine.enroll('SMOKE', 'native-helper-check')
        engine.receive('SMOKE', '1.1', '1.1', 'human', 'Test native helper integration only.')
        with engine.store.tx() as tx:
            for request in tx.all('requests'):
                request['status'] = 'done'
                tx.put('requests', request)
            task = engine.create_task(tx, dict(project=channel['project'], channel='SMOKE', thread='1.1', role='supervisor'),
                dict(role='engineer', goal='Integration check only: use Agent exactly once with subagent_type lab-researcher, model haiku, effort low. Ask it to answer 2+2 in one sentence without tools. Wait for its result. The platform saves the helper final text; read helpers/native-results/index.json and the listed findings.md, then write your own report.md confirming its answer and the saved path. Return completed with report.md as evidence. Do not ask the helper to write a report. No experiments, no other tools except writing your report, no follow-ups.',
                     acceptance='The native helper actually returns 4 and its response is saved in report.md.',
                     model='haiku', effort='low', session_minutes=2,
                     budget=dict(minutes=3, sessions=1, children=1, output_tokens=5000), requires_verification=False))
        start = time.time()
        try:
            while time.time()-start < 145:
                runtime.tick()
                sessions = engine.store.all('sessions')
                if sessions and sessions[0]['status'] not in ('running', 'stopping'):
                    session = sessions[0]
                    workspace = Path(session['workspace'])
                    state_path = workspace/'control/native-state.json'
                    state = json.loads(state_path.read_text()) if state_path.exists() else {}
                    saved=[a for a in state.get('agents',{}).values() if a.get('report') and (workspace/a['report']).is_file()]
                    passed = session['status'] == 'done' and len(state.get('invocations', {})) == 1 and any(a['status']=='done' for a in saved) and len(saved)==1 and '4' in (workspace/saved[0]['report']).read_text()
                    print(json.dumps(dict(passed=passed, session_status=session['status'], error=session.get('error'),
                         helper_invocations=len(state.get('invocations', {})), automatically_saved_reports=len(saved), helper_statuses=[a['status'] for a in state.get('agents', {}).values()],
                         usage=session.get('usage'), elapsed_seconds=round(time.time()-start, 1)), indent=2))
                    if not passed:
                        for field in ('stdout', 'stderr'):
                            path = Path(session.get(field, '/nonexistent'))
                            if path.exists():
                                # Explicit session text only; never print credential/debug files.
                                print(field + ': ' + path.read_text()[-6000:])
                    return 0 if passed else 1
                time.sleep(.25)
            print('Native check timed out; no further sessions will be launched.')
            return 1
        finally:
            for session in engine.store.all('sessions'):
                runtime.runner.terminate(session, hard=True)
            for proc in runtime.runner.processes.values():
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass


if __name__ == '__main__':
    raise SystemExit(main())
