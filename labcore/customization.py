"""Account-independent project memory and explicitly shared plugin packages."""
import hashlib
import json
import re
from pathlib import Path

from . import accounts


def memory_directory(engine, project):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', project):
        raise ValueError('Invalid memory project ID')
    return engine.root / 'runtime/claude-shared/memory' / project


def prepare(engine, session, native_flags):
    flags = list(native_flags)
    settings = {'disableAllHooks': True}
    if '--settings' in flags:
        index = flags.index('--settings')
        settings = json.loads(flags[index+1])
        del flags[index:index+2]
    if engine.cfg['claude'].get('shared_memory', True):
        directory = memory_directory(engine, session['project'])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings.update(autoMemoryEnabled=True, autoMemoryDirectory=str(directory))
    plugin_paths = []
    for raw in engine.cfg['claude'].get('shared_plugin_dirs', []):
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = engine.root / path
        path = path.resolve()
        if not path.is_dir():
            continue
        # CLI accepts a package or a directory whose children are packages.
        packages = [path] if (path/'.claude-plugin/plugin.json').is_file() else [p for p in sorted(path.iterdir()) if p.is_dir() and not p.name.startswith('.')]
        for package in packages:
            flags += ['--plugin-dir', str(package)]
            plugin_paths.append(str(package))
    session['shared_plugin_paths'] = plugin_paths
    return flags + ['--settings', json.dumps(settings)]


def import_memory(engine):
    """Preserve existing per-session memory under indexed, collision-free names."""
    if not engine.cfg['claude'].get('shared_memory', True):
        return 0
    copied = 0
    roots = [engine.root/'runtime/claude-auth/config']
    roots += [accounts.directory(engine.cfg, p['id']) for p in accounts.profiles(engine.cfg)]
    for session in engine.store.all('sessions'):
        workspace = str(Path(session['workspace']).resolve())
        encoded = re.sub(r'[^A-Za-z0-9]', '-', workspace)
        target = memory_directory(engine, session['project'])
        for root in set(roots):
            source = root/'projects'/encoded/'memory'
            if not source.is_dir() or source.is_symlink():
                continue
            for original in source.rglob('*.md'):
                if original.is_symlink() or not original.resolve().is_relative_to(source.resolve()):
                    continue
                content = original.read_bytes()
                if not content.strip():
                    continue
                source_id = hashlib.sha256(str(source).encode()).hexdigest()[:16]
                relative = original.relative_to(source)
                destination = target/'imported'/source_id/relative
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if destination.exists():
                    continue
                with destination.open('xb') as out:
                    out.write(content)
                destination.chmod(0o600)
                link = str(destination.relative_to(target))
                index = target/'MEMORY.md'
                with index.open('a') as out:
                    out.write(f'\n- [{original.name}]({link}) — imported prior session memory; review against current work orders.\n')
                index.chmod(0o600)
                copied += 1
    return copied
