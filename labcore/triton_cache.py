"""Triton cache publication compatible with Landlock ABI 1.

ABI 1 denies reparenting even within an otherwise writable tree. Keep each
atomic temporary file in its destination directory. This is selected through
Triton's cache-manager extension, not a patch to PyTorch or its kernels.
"""
import os
import tempfile
from pathlib import Path

from triton.runtime.cache import FileCacheManager


class SameDirectoryCacheManager(FileCacheManager):
    def put(self, data, filename, binary=True):
        if not self.cache_dir or self.lock_path is None:
            raise RuntimeError('Triton cache directory is unavailable')
        target = Path(self.cache_dir) / filename
        if not target.resolve().is_relative_to(Path(self.cache_dir).resolve()):
            raise ValueError('Cache entry escapes its cache directory')
        target.parent.mkdir(parents=True, exist_ok=True)
        binary = isinstance(data, bytes)
        fd, temporary = tempfile.mkstemp(prefix='.triton-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb' if binary else 'w') as out:
                out.write(data if binary else str(data))
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return str(target)
