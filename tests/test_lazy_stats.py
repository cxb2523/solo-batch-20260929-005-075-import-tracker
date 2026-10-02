"""
Acceptance tests for LazyImport observability statistics.

Three required groups are covered:

* first-time lazy import is timed and counted once;
* pre-imported modules resolve as cached with ``duration_ms == 0``;
* concurrent first resolution performs the real import exactly once.

Additional tests pin down in-progress snapshots, re-entrancy blocking, the
atexit persistence and the browser stats page.
"""

# Standard
import importlib
import json
import os
import sys
import threading
import time
import warnings

# Third Party
import pytest
import urllib.request

# Local
from import_tracker import lazy
from import_tracker.lazy import LazyImport, snapshot
from import_tracker.lazy_stats_serve import create_server


def _write_module(directory, name, body):
    """Write a top-level importable module file into ``directory``."""
    path = os.path.join(directory, f"{name}.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


@pytest.fixture
def on_path(tmp_path):
    """Put a temporary directory on sys.path for ad-hoc test modules."""
    sys.path.insert(0, str(tmp_path))
    try:
        yield tmp_path
    finally:
        sys.path.remove(str(tmp_path))


def _row(module_name):
    rows = {row["module"]: row for row in snapshot()["modules"]}
    assert module_name in rows, (
        f"no stats row for {module_name!r}: cached resolutions must not be dropped"
    )
    return rows[module_name]


############################## First-time import ##############################
def test_first_import_recorded(on_path):
    _write_module(on_path, "stats_first_mod", "VALUE = 1\n")
    proxy = LazyImport("stats_first_mod")
    assert proxy.VALUE == 1

    row = _row("stats_first_mod")
    assert row["status"] == "complete"
    assert row["cached"] is False
    assert row["count"] == 1
    assert row["duration_ms"] >= 0.0
    assert snapshot()["in_progress"] == []

    # Subsequent accesses reuse the resolved module without new stats rows.
    assert proxy.VALUE == 1
    assert proxy.stats()["hits"] == 1
    assert _row("stats_first_mod")["count"] == 1


def test_multiple_instances_dedup_module_summary(on_path):
    _write_module(on_path, "stats_dedup_mod", "VALUE = 2\n")
    first = LazyImport("stats_dedup_mod")
    second = LazyImport("stats_dedup_mod")
    assert first.VALUE == 2
    assert second.VALUE == 2

    rows = [row for row in snapshot()["modules"] if row["module"] == "stats_dedup_mod"]
    assert len(rows) == 1
    assert rows[0]["count"] == 1
    assert rows[0]["instances"] == 2
    assert first.stats()["hits"] == 1
    assert second.stats()["hits"] == 1


############################### Cached import ################################
def test_pre_imported_is_cached_zero_duration(on_path):
    _write_module(on_path, "stats_cached_mod", "VALUE = 3\n")
    importlib.import_module("stats_cached_mod")

    proxy = LazyImport("stats_cached_mod")
    assert proxy.VALUE == 3

    row = _row("stats_cached_mod")
    assert row["cached"] is True
    assert row["duration_ms"] == 0.0
    assert row["count"] == 0
    assert row["status"] == "complete"
    assert proxy.stats()["resolved"] is True


def test_cached_after_first_lazy_load(on_path):
    _write_module(on_path, "stats_cached_after_mod", "VALUE = 4\n")
    LazyImport("stats_cached_after_mod").VALUE
    second = LazyImport("stats_cached_after_mod")
    assert second.VALUE == 4

    row = _row("stats_cached_after_mod")
    assert row["count"] == 1
    assert row["hits"] == 2
    assert second.stats()["hits"] == 1


########################### Concurrent first import ##########################
def test_concurrent_first_import_happens_once(on_path):
    module_name = "stats_concurrent_mod"
    marker = str(on_path / f"{module_name}.touch")
    _write_module(
        on_path,
        module_name,
        (
            "import os, time\n"
            f"with open({marker!r}, 'a', encoding='utf-8') as _h:\n"
            "    _h.write('x\\n')\n"
            "time.sleep(0.3)\n"
            "VALUE = 5\n"
        ),
    )

    proxies = [LazyImport(module_name), LazyImport(module_name)]
    values = []
    barrier = threading.Barrier(2)
    errors = []

    def worker(proxy):
        try:
            barrier.wait()
            values.append(proxy.VALUE)
        except BaseException as err:  # noqa: BLE001 - surface all failures
            errors.append(err)

    threads = [threading.Thread(target=worker, args=(proxy,)) for proxy in proxies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert values == [5, 5]
    with open(marker, encoding="utf-8") as handle:
        assert handle.read().count("x") == 1

    row = _row(module_name)
    assert row["count"] == 1
    assert row["cached"] is False
    assert row["instances"] == 2


########################## Snapshot during import ############################
def test_snapshot_during_import_does_not_block_or_complete(on_path):
    module_name = "stats_inflight_mod"
    started = threading.Event()
    release = threading.Event()
    _write_module(
        on_path,
        module_name,
        (
            "import threading, time\n"
            f"_EVENT = {started!r}\n"
            f"_RELEASE = {release!r}\n"
            "import import_tracker.lazy as _stats_lazy  # same dir-independent name\n"
        ),
    )
    # Rebuild the body using injected events through a global rendezvous:
    # a dedicated rendezvous module is simpler than serializing event objects.
    rendezvous = on_path / "_stats_rendezvous.py"
    rendezvous.write_text(
        "import threading\nstarted = threading.Event()\nrelease = threading.Event()\n",
        encoding="utf-8",
    )
    _write_module(
        on_path,
        module_name,
        (
            "import time\n"
            "import _stats_rendezvous as r\n"
            "r.started.set()\n"
            "r.release.wait(5)\n"
            "VALUE = 6\n"
        ),
    )

    result = {}

    def importer():
        result["value"] = LazyImport(module_name).VALUE

    thread = threading.Thread(target=importer)
    thread.start()
    try:
        started.wait(2)
        # Snapshotting while the import is in flight must neither block nor
        # report the pending timing as a completed row.
        snap = snapshot()
        assert module_name in snap["in_progress"]
        row = {row["module"]: row for row in snap["modules"]}[module_name]
        assert row["status"] == "loading"
        assert row["duration_ms"] is None
        assert row["count"] == 0
    finally:
        release.set()
        thread.join()

    assert result["value"] == 6
    row = _row(module_name)
    assert row["status"] == "complete"
    assert row["count"] == 1
    assert snapshot()["in_progress"] == []


############################## Re-entrant import #############################
def test_reentrant_import_during_load_is_blocked(on_path):
    module_name = "stats_reentrant_mod"
    _write_module(
        on_path,
        module_name,
        (
            "from import_tracker.lazy import LazyImport\n"
            f"_proxy = LazyImport({module_name!r})\n"
            "_proxy.VALUE  # re-enters while this module body is importing\n"
            "VALUE = 7\n"
        ),
    )
    with pytest.raises(RuntimeError, match="Re-entrant lazy import"):
        LazyImport(module_name).VALUE


############################## Atexit dump ##################################
def test_dump_stats_writes_json(stats_dump_path):
    LazyImport("json").dumps
    lazy.dump_stats(str(stats_dump_path))
    data = json.loads(stats_dump_path.read_text(encoding="utf-8"))
    assert any(row["module"] == "json" for row in data["modules"])


def test_dump_on_exit_disabled_when_opted_out(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMPORT_TRACKER_LAZY_STATS", "0")
    LazyImport("csv").reader
    lazy._dump_on_exit()
    assert not (tmp_path / "lazy_import_stats.json").exists()


def test_dump_failure_only_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("IMPORT_TRACKER_LAZY_STATS", "1")
    lazy.configure_dump(enabled=True, path=str(tmp_path / "no_such_dir" / "x.json"))
    LazyImport("csv").reader
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lazy._dump_on_exit()
    assert any("Failed to write lazy import stats" in str(item.message) for item in caught)


############################### Browser page #################################
def test_stats_page_serves_table_and_json():
    # csv is a fresh real import here; json is cached if already imported.
    LazyImport("csv").reader
    server = create_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert any(row["module"] == "csv" for row in payload["modules"])

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        assert "Lazy import statistics" in html
        assert "csv" in html
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
