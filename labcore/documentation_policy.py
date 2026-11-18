"""Hard bounds for study documentation; no model calls or automatic rewrites."""
import re
from .store import uid

PHASES = {'technical_record','draft','revise','check'}
OPEN = {'queued','running','waiting_jobs','waiting_children','awaiting_review','partial','needs_resources','blocked','deferred'}


def compact_context(context):
    """A study handoff, with full original context linked on disk; no model summary."""
    task=context['task'];related={task['id'],*(task.get('depends') or []),*(task.get('task_ids') or [])}
    if task.get('verification_target'):related.add(task['verification_target'])
    for study in context.get('studies',[]):related.update(study.get('task_ids') or [])
    peers=[t for t in context.get('tasks',[]) if t['id']!=task['id'] and (t['id'] in related or
           (task.get('study_id') and t.get('study_id')==task['study_id']))]
    context['tasks']=[dict({k:t.get(k) for k in ('id','role','status','report')},summary=str(t.get('summary') or '')[:800])
                      for t in peers[-8:]]
    # The full current task is already present: don't include its long work order
    # twice more as a peer summary and the root allocation.
    allocation=context.get('root_allocation') or {}
    context['root_allocation']={k:allocation[k] for k in ('id','budget','resources') if k in allocation}
    story=context.get('research_story') or {}
    context['research_story']={k:story[k] for k in ('path','updated') if k in story}
    context['conversation']=[]
    context['scheduled_reviews']=[]
    context['other_projects']=[]
    context['documentation_handoff']={
        'mode':'saved_artifacts', 'checkpoint':'checkpoint.json', 'previous_result':'control/last-result.json',
        'full_assignment_and_messages':'control/context.json',
        'instruction':'Continue existing drafts and checked evidence. Read specific original constraints when needed; do not reconstruct the whole project or repeat unchanged checks.'}
    return context


def is_documentation(task):
    if task.get('work_kind') == 'documentation':return True
    lead=task.get('goal','')[:260].lower()
    named=bool(re.search(r'paper[_ -]section|detailed[_ -]report|study[_ -]report|report with (?:an? )?appendix',lead))
    return named and (task.get('role')=='literature' or bool(re.match(r'(produce|write|revise|typeset|update|finish)\b',lead))) or (
        task.get('role')=='verifier' and 'documentation' in lead and 'verif' in lead)


def profile_issue(cfg,task):
    policy=cfg.get('documentation',{})
    if not policy.get('enabled',False) or not is_documentation(task):return None
    if not task.get('study_id') or task.get('document_phase') not in PHASES:
        return 'Documentation hold: use one study_id and an explicit document_phase; legacy bulk assignments cannot resume'
    phase=task['document_phase']
    format=policy.get('report_format')
    kinds=('report',) if format=='single_with_appendix' else ('paper','detailed') if format=='legacy_pair' else ('paper','detailed','report')
    if phase in ('draft','revise') and task.get('document_kind') not in kinds:
        return 'Documentation hold: a writing assignment must target exactly one PDF in the configured report format'
    if phase in ('technical_record','check') and task.get('document_kind'):
        return 'Documentation hold: technical-record and check phases do not take a document_kind'
    if phase=='technical_record' and task.get('role')!='engineer':return 'Technical records must be assigned to engineering'
    if phase=='check' and task.get('role')!='verifier':return 'Documentation checks must be assigned to a verifier'
    if phase in ('draft','revise') and task.get('role')!='literature':return 'PDF writing must use the literature writer role'
    model_ceiling=policy.get('review_model_ceiling','opus') if phase in ('technical_record','check') else policy.get('writer_model_ceiling','sonnet')
    effort_ceiling=policy.get('review_effort_ceiling','high') if phase in ('technical_record','check') else policy.get('writer_effort_ceiling','medium')
    models=('haiku','sonnet','opus');efforts=('low','medium','high','xhigh')
    if task.get('model') not in models or models.index(task['model'])>models.index(model_ceiling):return 'Documentation model exceeds the configured phase ceiling'
    if task.get('effort') not in efforts or efforts.index(task['effort'])>efforts.index(effort_ceiling):return 'Documentation effort exceeds the configured phase ceiling'
    budget=task.get('budget',{})
    if budget.get('children',0)>0:return 'Documentation children budget exceeds the configured cap'
    if task.get('resources',{}).get('max_gpus',0)>0:return 'Documentation assignments cannot authorize GPU jobs'
    return None


def launch_issue(engine,tx,task,*,ignore_session=None):
    if not engine.cfg.get('documentation',{}).get('enabled',False) or not is_documentation(task):return None
    issue=profile_issue(engine.cfg,task)
    if issue:return issue
    lane=tx.get('documentation_focus',task['project'],{})
    focus=lane.get('study_id')
    if task.get('document_cycle') != lane.get('cycle') or not lane.get('cycle'):
        return 'Documentation hold: this assignment belongs to an older or unselected publication cycle'
    if focus!=task['study_id']:return 'Documentation hold: this study is not the selected publication focus'
    study=tx.get('studies',task['project']+':'+task['study_id'])
    if not study:return 'Documentation hold: register the study first'
    if any(s.get('id')!=ignore_session and s.get('status') in ('running','stopping') and s.get('project')==task['project'] and
           is_documentation(tx.get('tasks',s.get('task',''),{})) for s in tx.all('sessions')):
        return 'Documentation hold: another documentation phase is active'
    # Continuations use the ordinary root-task budget; no extra cross-task phase cap.
    phase=task['document_phase']
    from .studies import current_view
    checks=current_view(engine.root,tx.get('projects',task['project']),study).get('checks',{});files=checks.get('files',{})
    if phase!='technical_record':
        source='technical_record' if study.get('technical_record') or 'technical_record' in files else 'source_record'
        if source not in files or any(i.startswith(source+':') for i in checks.get('issues',[])):
            return 'Documentation hold: the canonical technical record must pass checks first'
    pdfs=('report_pdf',) if study.get('report_pdf') and engine.cfg.get('research_story',{}).get('format')!='study_table' else ('paper_pdf','detailed_pdf')
    if phase=='check' and not all(k in files for k in pdfs):
        return 'Documentation hold: register the report draft before its review'
    if phase=='check' and any(i.startswith(tuple(k+':' for k in pdfs)) for i in checks.get('issues',[])):
        return 'Documentation hold: PDF drafts must pass file checks before their review'
    if phase=='revise' and (task['document_kind']+'_pdf') not in files:
        return 'Documentation hold: register the existing PDF before assigning a revision'
    return None


def select_focus(tx,project,study_id,*,new_cycle=False,root=None):
    if not tx.get('studies',project+':'+study_id):raise ValueError('Register the study before selecting its publication focus')
    old=tx.get('documentation_focus',project,{})
    if old.get('study_id') and (old['study_id']!=study_id or new_cycle):
        previous=tx.get('studies',project+':'+old['study_id'],{})
        if root is not None:
            from .studies import current_view
            previous=current_view(root,tx.get('projects',project),previous)
        if previous.get('documentation') not in ('complete','published_with_gaps'):
            raise ValueError('Finish the current publication package before selecting the next study')
        if any(t.get('project')==project and t.get('study_id')==old['study_id'] and is_documentation(t) and
               t.get('status') in OPEN for t in tx.all('tasks')):
            raise ValueError('Resolve the current study’s open documentation assignments before advancing')
    if old.get('study_id')==study_id and not new_cycle and old.get('cycle'):return
    cycle=uid()
    tx.put('documentation_focus',dict(id=project,project=project,study_id=study_id,cycle=cycle))
    for task in tx.all('tasks'):
        if (task.get('project')==project and task.get('study_id')==study_id and is_documentation(task)
                and task.get('status') in ('queued','deferred') and task.get('document_cycle') is None
                and not task.get('attempts')):
            task['document_cycle']=cycle
            task.pop('launch_hold',None)
            tx.put('tasks',task)
