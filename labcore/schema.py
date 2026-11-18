"""Provider-compatible closed schemas; action authorization also lives in Engine."""

def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


STRING = {"type": "string"}
NUMBER = {"type": "number"}
BOOL = {"type": "boolean"}
STRINGS = {"type": "array", "items": STRING}
ACTION_SCHEMA = obj({
    "type": {"type": "string", "enum": ["set_project", "assign", "grant", "answer", "reprioritize", "accept", "reject",
        "meeting", "decision", "share_file", "promote_code", "cancel", "pause_project", "close_thread", "link_channel",
        "spawn", "clarification", "escalate", "publish_paper", "publish_digest", "write_note", "make_folder", "schedule_wake", "cancel_wake", "defer_task", "update_study", "set_documentation_focus", "propose_research_notes", "edit_research_notes"]},
    "recurrence": {"type": "string", "enum": ["once", "weekly"]},
    "timezone": STRING,
    "digest_record": obj({**{k: STRING for k in ('topic', 'title', 'period_start', 'period_end', 'summary', 'text')},
        'sections': {'type':'array','items':obj({'topic':STRING,'summary':STRING,'papers':{'type':'array','items':obj(
            {k:STRING for k in ('title','url','summary','details','canonical_id')},required=('title','url','summary','details'))}},required=('topic','papers'))}}),
    **{key: STRING for key in ("mode", "purpose", "brief", "name", "task", "verification_target", "project", "key",
        "role", "goal", "acceptance", "model", "effort", "text", "reason", "question", "relevance", "path", "title", "disagreement",
        "at", "wake_id", "usage_class", "not_before", "study_id", "work_kind", "document_phase", "document_kind")},
    "study_record": obj({**{k: STRING for k in ('title','question','status_note','execution','verification','verification_task','technical_record','source_record','report_pdf','paper_pdf','detailed_pdf','story_draft','consistency_record','qa_record','review_notes')},
                         **{k: STRINGS for k in ('task_ids','figures','known_gaps')}}),
    "notes_change": obj({'note_id':STRING,'base_revision':{'type':'integer','minimum':0},'section':STRING,'mode':{'type':'string','enum':['replace','append','comment']},'replacement':STRING,'comment':STRING,'summary':STRING,'figure_paths':STRINGS},('base_revision','mode')),
    "grant_id": STRING,
    "publish": BOOL, "new_cycle": BOOL,
    "session_minutes": NUMBER, "priority": NUMBER, "turns": NUMBER, "delay_minutes": NUMBER,
    **{key: BOOL for key in ("requires_verification", "allow_job_followup", "allow_child_followup", "direct_reply", "paused")},
    **{key: STRINGS for key in ("depends", "paths", "sources")},
    "budget": obj({key: NUMBER for key in ("minutes", "sessions", "children")}),
    "resources": obj({key: NUMBER for key in ("max_gpus", "max_cpu_threads", "max_job_hours", "total_job_hours", "max_jobs")}),
    "request": obj({"model": STRING, "effort": STRING, **{key: NUMBER for key in ("minutes", "sessions", "gpus", "cpu_threads", "compute_hours")}}),
}, ("type",))

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "urgent": {"type": "boolean"},
        "urgent_reason": {"type": "string"},
        "status": {"type": "string", "enum": ["completed", "partial", "blocked", "waiting_jobs", "waiting_children", "needs_resources"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "description": {"type": "string"}}, "required": ["path", "description"], "additionalProperties": False}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string", "enum": ["pass", "fail", "not_applicable"]},
        "resolved": {"type": "boolean"},
        "actions": {"type": "array", "items": ACTION_SCHEMA},
    },
    "required": ["reply", "status", "summary", "findings", "limitations", "evidence", "next_steps", "actions"],
    "additionalProperties": False,
}
