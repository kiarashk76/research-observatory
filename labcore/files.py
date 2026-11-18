import hashlib
import json
import os
import re
from pathlib import Path


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2))
    temp.replace(path)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "project"


def paper_folder(canonical):
    return slug(canonical) + "-" + hashlib.sha256(canonical.encode()).hexdigest()[:10]


def scoped(base, relative, *, exists=True):
    base = Path(base).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError("Artifact path escapes its assigned workspace")
    if any(p in {".claude", ".auth", "control", "tmp"} for p in path.relative_to(base).parts):
        raise ValueError("Runtime/auth/control files cannot be published as research artifacts")
    if exists and not path.exists():
        raise ValueError(f"Missing artifact: {relative}")
    return path


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def project_layout(root, project):
    base = Path(root) / "projects" / project["folder"]
    for sub in ("notes", "decisions", "experiments", "code", "runs", "reports"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    (base / "project.json").write_text(json.dumps(project, indent=2))
    return base
