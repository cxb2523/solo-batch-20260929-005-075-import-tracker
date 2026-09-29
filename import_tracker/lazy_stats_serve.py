"""
Read-only browser view of :mod:`import_tracker.lazy` statistics.

Running ``python -m import_tracker.lazy_stats_serve`` starts a small,
dependency-free HTTP server (the same shape as the project's other stdlib-based
serving examples) that renders the module-level lazy import summary as a table.
It is a library entry point rather than a throwaway script:
:func:`make_handler` can be mounted on any ``socketserver``-style server and
:func:`create_server` returns a ready-to-serve instance.

The page auto-refreshes, so lazy imports that resolve while the server runs
show up without a restart.
"""

# Standard
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Local
from . import lazy

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>import_tracker lazy import stats</title>
<meta http-equiv="refresh" content="2">
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.3rem; }}
  table {{ border-collapse: collapse; margin-top: 1rem; min-width: 640px; }}
  th, td {{ border: 1px solid #ccc; padding: 0.4rem 0.8rem; text-align: left; }}
  th {{ background: #f2f2f2; }}
  tr.loading td {{ font-style: italic; color: #777; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .tag {{ font-size: 0.75rem; padding: 0.1rem 0.4rem; border-radius: 3px; }}
  .tag-complete {{ background: #e2f4e2; }}
  .tag-loading {{ background: #fff4d6; }}
  .tag-error {{ background: #fbe2e2; }}
  code {{ background: #f4f4f4; padding: 0 0.25rem; }}
</style>
</head>
<body>
<h1>Lazy import statistics</h1>
<p>Auto-refreshes every 2 seconds. JSON: <code>/api/stats</code></p>
<table>
<thead>
<tr>
  <th>module</th><th>status</th><th>cached</th>
  <th class="num">imports</th><th class="num">hits</th>
  <th class="num">instances</th><th class="num">duration_ms</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>
"""

_ROW_TEMPLATE = (
    '<tr class="{status_cls}"><td>{module}</td>'
    '<td><span class="tag tag-{status}">{status}</span></td>'
    "<td>{cached}</td>"
    '<td class="num">{count}</td><td class="num">{hits}</td>'
    '<td class="num">{instances}</td>'
    '<td class="num">{duration}</td></tr>'
)

EMPTY_ROW = '<tr><td colspan="7">No lazy imports recorded yet.</td></tr>'


def render_page(stats: Dict[str, Any]) -> str:
    """Render the statistics snapshot as an HTML table page."""
    if not stats["modules"]:
        rows = EMPTY_ROW
    else:
        rows = "\n".join(
            _ROW_TEMPLATE.format(
                module=row["module"],
                status=row["status"],
                status_cls="loading" if row["status"] == "loading" else "",
                cached="yes" if row["cached"] else "no",
                count=row["count"],
                hits=row["hits"],
                instances=row["instances"],
                duration="" if row["duration_ms"] is None else row["duration_ms"],
            )
            for row in stats["modules"]
        )
    return PAGE_TEMPLATE.format(rows=rows)


def seed_demo_stats() -> None:
    """Resolve a few lazy imports so a freshly opened page has rows.

    One module is imported normally first to demonstrate a cached row with
    ``duration_ms == 0.0``; another is resolved through two separate
    :class:`LazyImport` instances to demonstrate the module-level de-duplication.
    """
    import json as pre_imported

    assert pre_imported is not None
    lazy.LazyImport("json").dumps
    lazy.LazyImport("csv").reader
    lazy.LazyImport("csv").writer


def make_handler() -> type:
    """Build a read-only request handler class for the live stats snapshot."""

    class LazyStatsHandler(BaseHTTPRequestHandler):
        """Serve the stats table page and the stats JSON payload."""

        server_version = "ImportTrackerLazyStats/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8", status)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                body = render_page(lazy.snapshot()).encode("utf-8")
                self._send(body, "text/html; charset=utf-8")
            elif parsed.path == "/api/stats":
                self._send_json(lazy.snapshot())
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.end_headers()

    return LazyStatsHandler


def create_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    seed: bool = False,
) -> ThreadingHTTPServer:
    """Build (but do not start) the read-only lazy stats HTTP server."""
    if seed:
        seed_demo_stats()
    return ThreadingHTTPServer((host, port), make_handler())


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for ``python -m import_tracker.lazy_stats_serve``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", default="127.0.0.1", help="Interface to bind to (default: loopback)."
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="TCP port to listen on (default: 8000)."
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Resolve a couple of demo lazy imports before serving.",
    )
    args = parser.parse_args(argv)

    server = create_server(host=args.host, port=args.port, seed=args.seed)
    host, port = server.server_address[:2]
    sys.stderr.write(f"Serving lazy import stats at http://{host}:{port}/ (read-only)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nShutting down.\n")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
