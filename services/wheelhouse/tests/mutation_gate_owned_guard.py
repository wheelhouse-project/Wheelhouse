"""Caller-side restoration interlock for the separately owned process runner."""
from contextlib import contextmanager
from functools import wraps
import hashlib
from pathlib import Path
import uuid
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.codex.owned_process import CleanupUnconfirmedError, run_owned


class MutationRun:
    def __init__(self, root, paths):
        self.evidence_dir = Path(root) / ".tmp/mutation-owned" / uuid.uuid4().hex
        self.evidence_dir.mkdir(parents=True)
        self.uncertain = None
        self.backups = {}
        for path in dict.fromkeys(map(Path, paths)):
            key = hashlib.sha256(str(path).encode()).hexdigest()[:16] + "-" + path.name
            backup = self.evidence_dir / (key + ".original")
            backup.write_bytes(path.read_bytes())
            self.backups[path] = backup

    def run(self, command, *, cwd, env, timeout):
        self.require_cleanup()
        try:
            return run_owned(command, cwd=cwd, env=env, timeout_seconds=timeout,
                             evidence_dir=self.evidence_dir)
        except CleanupUnconfirmedError as exc:
            self.uncertain = exc
            # Original backups already exist; even failed evidence writes cannot
            # clear the interlock or permit restoration over a live descendant.
            for path, backup in self.backups.items():
                try:
                    backup.with_suffix(".mutant").write_bytes(path.read_bytes())
                except OSError:
                    pass
            raise

    def subprocess_run(self, command, *, cwd, env, timeout, capture_output, text):
        """Narrow adapter for the existing PTT gate's captured subprocess API."""
        if capture_output is not True or text is not True:
            raise ValueError("mutation test adapter requires captured text")
        return self.run(command, cwd=cwd, env=env, timeout=timeout)

    def require_cleanup(self):
        if self.uncertain is not None:
            raise self.uncertain

    @contextmanager
    def protect(self, module, names):
        originals = {name: getattr(module, name) for name in names}
        cleanup_dispatch = getattr(module, "_call_cleanup", None)
        wrapped = {}
        def guarded(function):
            @wraps(function)
            def call(*args, **kwargs):
                self.require_cleanup()
                return function(*args, **kwargs)
            return call
        try:
            for name, function in originals.items():
                call = guarded(function)
                wrapped[call] = function
                setattr(module, name, call)
            if cleanup_dispatch is not None:
                @wraps(cleanup_dispatch)
                def dispatch(call, *args):
                    self.require_cleanup()
                    # The existing dispatcher must inspect the cleanup body's
                    # entry frame, not the already-entered ownership wrapper.
                    return cleanup_dispatch(wrapped.get(call, call), *args)
                module._call_cleanup = dispatch
            yield
        finally:
            for name, function in originals.items():
                setattr(module, name, function)
            if cleanup_dispatch is not None:
                module._call_cleanup = cleanup_dispatch
