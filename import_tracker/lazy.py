"""
Lazy module imports with observability statistics.

A :class:`LazyImport` defers the underlying ``importlib.import_module`` call
until the first attribute access on the instance. Every resolution is recorded
in a process-wide statistics registry (the module-level summary) and on the
instance that triggered it.

Statistics semantics:

* ``duration_ms`` measures only the real ``importlib.import_module`` call made
  from :meth:`LazyImport.__getattr__`. No import hooks are involved.
* If the module is already present in ``sys.modules`` when it is first resolved,
  the row is recorded as ``cached`` with ``duration_ms == 0.0`` and ``count`` of
  zero real lazy imports. Cached resolutions are always retained in snapshots.
* Two threads racing for the first resolution of the same module perform the
  real import exactly once and produce exactly one recorded import.
* Snapshots never take the import lock and therefore never block a first
  import; modules still being imported are exposed as ``in_progress`` rather
  than as completed rows.
"""

# Standard
import atexit
import importlib
import importlib.util
import json
import os
import sys
import threading
import time
import warnings
import weakref
from typing import Any, Dict, List, Optional

# Environment knobs for the atexit persistence of the stats
DUMP_ENABLE_ENV = "IMPORT_TRACKER_LAZY_STATS"
DUMP_PATH_ENV = "IMPORT_TRACKER_LAZY_STATS_FILE"
DEFAULT_DUMP_PATH = "lazy_import_stats.json"


def _env_truthy(value: Optional[str]) -> bool:
    """Return whether an environment value opts out (0/false/off/no)."""
    return value is None or value.strip().lower() not in ("0", "false", "off", "no", "")


class _StatsRegistry:
    """Process-wide registry of lazy import statistics.

    The stats lock is only ever held for short bookkeeping sections. It is
    never held while the real ``importlib.import_module`` runs, so taking a
    snapshot cannot block a first import and the stats lock cannot deadlock
    against the import lock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: Dict[str, Dict[str, Any]] = {}
        self._import_locks: Dict[str, threading.Lock] = {}
        self._import_owners: Dict[str, int] = {}
        self._module_instances: Dict[str, "weakref.WeakSet[LazyImport]"] = {}

    def reset(self) -> None:
        """Clear all recorded statistics (primarily intended for testing)."""
        with self._lock:
            self._records.clear()
            self._import_locks.clear()
            self._import_owners.clear()
            self._module_instances.clear()

    def _record_locked(self, module_name: str) -> Dict[str, Any]:
        record = self._records.get(module_name)
        if record is None:
            record = {
                "module": module_name,
                "status": "pending",
                "cached": False,
                "count": 0,
                "hits": 0,
                "instances": 0,
                "duration_ms": None,
                "error": None,
            }
            self._records[module_name] = record
        return record

    def resolve(self, instance: "LazyImport") -> Any:
        """Resolve ``instance`` and record statistics for the resolution."""
        module_name = instance._resolved_name
        thread_id = threading.get_ident()

        # Register the access. This is the only bookkeeping done before the
        # import and it never holds the stats lock across an import.
        with self._lock:
            record = self._record_locked(module_name)
            record["hits"] += 1
            instances = self._module_instances.setdefault(module_name, weakref.WeakSet())
            instances.add(instance)

        while True:
            wait_lock: Optional[threading.Lock] = None
            with self._lock:
                import_lock = self._import_locks.get(module_name)
                if import_lock is None:
                    # A freshly created lock is uncontended, so acquiring it
                    # here while holding the stats lock cannot block.
                    import_lock = threading.Lock()
                    import_lock.acquire()
                    self._import_locks[module_name] = import_lock
                    self._import_owners[module_name] = thread_id
                elif self._import_owners.get(module_name) == thread_id:
                    # The same thread is already importing this module and
                    # re-enters the lazy attribute. Block it explicitly rather
                    # than waiting on a lock it already owns (which would
                    # deadlock).
                    raise RuntimeError(
                        f"Re-entrant lazy import of {module_name!r} detected "
                        "while the module is still being imported"
                    )
                else:
                    wait_lock = import_lock

            if wait_lock is not None:
                # Wait for the in-flight import without holding the stats lock.
                wait_lock.acquire()
                wait_lock.release()
                continue

            try:
                with self._lock:
                    status = record["status"]

                if status == "complete":
                    return sys.modules[module_name]

                if module_name in sys.modules:
                    # Imported by someone else (or by a prior lazy instance):
                    # this is a cache hit, no real import is timed.
                    with self._lock:
                        record["status"] = "complete"
                        record["cached"] = True
                        record["duration_ms"] = 0.0
                    return sys.modules[module_name]

                # This is the one real import. Timing covers only this call.
                start = time.perf_counter()
                try:
                    resolved = importlib.import_module(module_name)
                except Exception as err:
                    with self._lock:
                        record["status"] = "error"
                        record["error"] = str(err)
                    raise
                duration_ms = (time.perf_counter() - start) * 1000.0
                with self._lock:
                    record["status"] = "complete"
                    record["cached"] = False
                    record["count"] += 1
                    record["duration_ms"] = round(duration_ms, 3)
                return resolved
            finally:
                with self._lock:
                    self._import_locks.pop(module_name, None)
                    self._import_owners.pop(module_name, None)
                import_lock.release()

    def snapshot(self) -> Dict[str, Any]:
        """Return a point-in-time, non-blocking copy of the statistics.

        Modules mid-import are listed under ``in_progress`` and their row keeps
        ``status == "loading"`` with ``duration_ms`` still ``None`` so an
        in-flight timer is never reported as a completed import.
        """
        with self._lock:
            in_progress = sorted(self._import_locks.keys())
            rows: List[Dict[str, Any]] = []
            for module_name, record in self._records.items():
                row = dict(record)
                if module_name in self._import_locks:
                    row["status"] = "loading"
                instances = self._module_instances.get(module_name)
                row["instances"] = len(instances) if instances is not None else 0
                rows.append(row)
            rows.sort(key=lambda row: row["module"])
            return {"modules": rows, "in_progress": in_progress}

    def instance_stats(self, instance: "LazyImport") -> Dict[str, Any]:
        """Return the per-instance view of the lazy import statistics."""
        return {
            "module": instance._resolved_name,
            "hits": instance._hits,
            "resolved": "_resolved_module" in instance.__dict__,
        }


_REGISTRY = _StatsRegistry()


class LazyImport:
    """Proxy that imports a module on the first attribute access."""

    def __init__(self, module: str, package: Optional[str] = None) -> None:
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_package", package)
        object.__setattr__(self, "_hits", 0)
        # Resolve relative import names up-front so stats keys are absolute.
        object.__setattr__(
            self,
            "_resolved_name",
            importlib.util.resolve_name(module, package),
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        if self.__dict__.get("_resolved_module") is None:
            object.__setattr__(self, "_hits", self._hits + 1)
            module = _REGISTRY.resolve(self)
            object.__setattr__(self, "_resolved_module", module)
        else:
            module = self.__dict__["_resolved_module"]
        return getattr(module, name)

    def stats(self) -> Dict[str, Any]:
        """Return the statistics recorded for this lazy import instance."""
        return _REGISTRY.instance_stats(self)

    def __repr__(self) -> str:
        return f"LazyImport({self._module!r})"


def snapshot() -> Dict[str, Any]:
    """Return a snapshot of the module-level lazy import statistics."""
    return _REGISTRY.snapshot()


def reset_stats() -> None:
    """Reset the module-level statistics registry."""
    _REGISTRY.reset()


# Whether/when the atexit persistence runs can be configured at import time
# through environment variables or later through :func:`configure_dump`.
_dump_enabled = _env_truthy(os.environ.get(DUMP_ENABLE_ENV))
_dump_path = os.environ.get(DUMP_PATH_ENV)


def configure_dump(enabled: Optional[bool] = None, path: Optional[str] = None) -> None:
    """Configure the atexit persistence of the lazy import statistics."""
    global _dump_enabled, _dump_path
    if enabled is not None:
        _dump_enabled = enabled
    if path is not None:
        _dump_path = path


def dump_stats(path: Optional[str] = None) -> None:
    """Write the current statistics to ``path`` as JSON."""
    target = path or _dump_path or DEFAULT_DUMP_PATH
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(snapshot(), handle, indent=2, sort_keys=True)
        handle.write("\n")


def _dump_on_exit() -> None:
    """Persist statistics at interpreter exit unless the feature is disabled.

    Nothing is written when no lazy imports were recorded, and write failures
    only emit a warning so exit codes and existing behavior are unaffected.
    """
    if not _dump_enabled:
        return
    current = snapshot()
    if not current["modules"]:
        return
    target = _dump_path or DEFAULT_DUMP_PATH
    try:
        dump_stats(target)
    except Exception as err:  # pragma: no cover - warning path
        warnings.warn(f"Failed to write lazy import stats to {target!r}: {err}")


atexit.register(_dump_on_exit)
