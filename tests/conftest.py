"""
Fixtures shared by the lazy stats acceptance tests.
"""

# Standard
import os
import sys

# Third Party
import pytest

# Local
from import_tracker import lazy


@pytest.fixture(autouse=True)
def isolate_lazy_stats(tmp_path, monkeypatch):
    """Keep stats tests hermetic.

    The atexit dump is disabled so test runs never leave files behind, and the
    process-wide registry is reset around each test.
    """
    monkeypatch.setenv("IMPORT_TRACKER_LAZY_STATS", "0")
    lazy.configure_dump(enabled=False)
    lazy.reset_stats()
    before_keys = set(sys.modules.keys())
    before_meta = list(sys.meta_path)
    yield
    lazy.reset_stats()
    lazy.configure_dump(enabled=False)
    for key in list(sys.modules.keys()):
        if key not in before_keys:
            sys.modules.pop(key, None)
    sys.meta_path[:] = before_meta


@pytest.fixture
def stats_dump_path(tmp_path, monkeypatch):
    """Point the atexit dump at a temporary file and enable it."""
    path = tmp_path / "lazy_stats.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IMPORT_TRACKER_LAZY_STATS", raising=False)
    lazy.configure_dump(enabled=True, path=str(path))
    return path
