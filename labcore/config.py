from __future__ import annotations

import os
import math
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"haiku", "sonnet", "opus"}
EFFORTS = {"low", "medium", "high", "xhigh"}
ROLES = {"supervisor", "engineer", "literature", "verifier", "worker"}


def load(root=None):
    root = Path(root or ROOT).resolve()
    cfg = yaml.safe_load((root / "config.yaml").read_text()) or {}
    cfg["root"] = str(root)
    if 'agent_backend' in cfg:
        raise ValueError('Only the Claude backend is supported; remove agent_backend configuration')
    from .accounts import profiles
    profiles(cfg)  # Reject ambiguous account IDs before the daemon starts.
    from .delegation import concurrency_limit
    concurrency_limit(cfg.get('native_subagents',{}))
    documentation=cfg.get('documentation',{})
    if documentation.get('report_format','single_with_appendix') not in ('single_with_appendix','legacy_pair'):
        raise ValueError('Invalid documentation.report_format')
    if 'resume_cli_history' in documentation and not isinstance(documentation['resume_cli_history'],bool):
        raise ValueError('documentation.resume_cli_history must be true or false')
    for name in ('max_build_minutes',):
        value=documentation.get(name,1)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError('documentation.'+name+' must be a positive finite number')
    for name,allowed in (('writer_model_ceiling',MODELS),('review_model_ceiling',MODELS),
                         ('writer_effort_ceiling',EFFORTS),('review_effort_ceiling',EFFORTS)):
        if name in documentation and documentation[name] not in allowed:raise ValueError('Invalid documentation.'+name)
    for section,name in (('budgets','max_jobs'),('budgets','task_jobs'),('resources','max_cpu_threads_per_job'),('resources','max_cpu_threads_per_assignment')):
        value=cfg.get(section,{}).get(name)
        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<1 or int(value)!=value):
            raise ValueError(section+'.'+name+' must be a positive integer')
    sharing=cfg.get('resources',{}).get('gpu_sharing',{})
    if 'enabled' in sharing and not isinstance(sharing['enabled'],bool):
        raise ValueError('resources.gpu_sharing.enabled must be true or false')
    for name in ('max_jobs_per_gpu','memory_headroom_mib','memory_headroom_fraction'):
        if name not in sharing:continue
        value=sharing[name]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise ValueError('Invalid resources.gpu_sharing.'+name)
        if name=='max_jobs_per_gpu' and (value<1 or int(value)!=value):
            raise ValueError('max_jobs_per_gpu must be a positive integer')
        if name=='memory_headroom_fraction' and value>=1:
            raise ValueError('memory_headroom_fraction must be less than 1')
    chat=cfg.get('research_chat',{})
    if not isinstance(chat,dict):raise ValueError('research_chat must be a mapping')
    if 'enabled' in chat and not isinstance(chat['enabled'],bool):raise ValueError('research_chat.enabled must be boolean')
    if chat.get('model','sonnet') not in MODELS or chat.get('effort','medium') not in EFFORTS:raise ValueError('Invalid research_chat model or effort')
    minutes=chat.get('max_session_minutes',120)
    if isinstance(minutes,bool) or not isinstance(minutes,(int,float)) or not math.isfinite(minutes) or minutes<=0:raise ValueError('research_chat.max_session_minutes must be positive')
    return cfg


def profile(model, effort):
    model = str(model).lower()
    effort = {"extra": "xhigh", "extra high": "xhigh"}.get(str(effort).lower(), str(effort).lower())
    if model not in MODELS or effort not in EFFORTS:
        raise ValueError("Use Haiku/Sonnet/Opus and low/medium/high/extra (xhigh)")
    return model, effort


def secrets(root):
    env = dict(os.environ)
    path = Path(root) / ".slack.env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env.setdefault(key.strip(), value.strip().strip("\"'"))
    if not env.get("SLACK_BOT_TOKEN") or not env.get("SLACK_APP_TOKEN"):
        raise ValueError("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .slack.env")
    return env["SLACK_BOT_TOKEN"], env["SLACK_APP_TOKEN"]
