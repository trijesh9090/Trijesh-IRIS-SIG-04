"""Keeps track of where a run is, and serves the --live page.

The page asks /status.json once a second and shows the six steps one by one. When the run is done it moves on
to the dashboard (report.html). The server only listens on 127.0.0.1.
"""
import json
import logging
import threading
import time
from collections import Counter, deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

log = logging.getLogger("harvester")

# the page itself lives in templates/live.html
LIVE_PAGE = (resources.files(__package__) / "templates" / "live.html").read_text(encoding="utf-8")

# the six steps in order, with the names shown on the page
STEPS = [("portal", "Load municipalities"), ("select", "Select towns"), ("crawl", "Extract notices"),
         ("dedupe", "Remove duplicates"), ("load", "Store in database"), ("report", "Build dashboard")]


class Progress:
    """Keeps track of where a run is: the state of the six steps and the crawl counters.

    The crawl workers update it from several threads, so every change goes through the lock.
    """

    def __init__(self, run_id: str):
        """Args:
            run_id: id of the current run, shown on the live page.
        """
        self.lock = threading.Lock()
        self.run_id = run_id
        self.steps = {k: {"step": k, "label": label, "state": "waiting", "detail": {}} for k, label in STEPS}
        self.crawl = {"total": 0, "done": 0, "notices": 0, "status": Counter(), "active": set(),
                      "recent": deque(maxlen=12)}
        self.finished, self.error = False, None
        self._started: dict[str, float] = {}  # when each step started, for its duration

    def begin(self, key: str, **detail):
        """Mark a step as running.

        Args:
            key: the step, e.g. "crawl".
            **detail: numbers to show next to the step while it runs.
        """
        with self.lock:
            self.steps[key].update(state="running", started=time.time(), detail=detail)
            self._started[key] = time.monotonic()

    def end(self, key: str, **detail):
        """Mark a step as done, note how long it took and log it.

        Args:
            key: the step.
            **detail: the step's result numbers, e.g. records=87, duplicates=4.
        """
        with self.lock:
            step = self.steps[key]
            seconds = round(time.monotonic() - self._started[key], 2)
            step.update(state="done", seconds=seconds, detail=detail)
        log.info("step %s done in %.1fs %s", key, seconds, detail)

    def fail(self, key: str, reason: str):
        """Mark a step as failed. The live page shows it in red.

        Args:
            key: the step.
            reason: short text saying what went wrong.
        """
        with self.lock:
            self.steps[key].update(state="failed", detail={"error": reason})
            self.error = f"{self.steps[key]['label']}: {reason}"

    def town_started(self, name: str):
        """Show a town under "Working on now" on the live page.

        Args:
            name: the town a worker just started on.
        """
        with self.lock:
            self.crawl["active"].add(name)

    def town_done(self, outcome: dict):
        """Count a finished town and add it to the "Just finished" list.

        Args:
            outcome: the town's outcome from harvest().
        """
        with self.lock:
            crawl = self.crawl
            crawl["active"].discard(outcome["municipality"])
            crawl["done"] += 1
            crawl["notices"] += outcome["notices"]
            crawl["status"][outcome["status"]] += 1
            crawl["recent"].appendleft({
                "municipality": outcome["municipality"],
                "status": outcome["status"],
                "reason": outcome["reason"],
                "notices": outcome["notices"],
            })

    def timings(self) -> list[dict]:
        """List the finished steps with their time and numbers. Saved in report.json and shown on the dashboard.

        Returns:
            e.g. [{"step": "crawl", "seconds": 616.6, "pages": 1783, ...}, ...]
        """
        with self.lock:
            finished = []
            for step in self.steps.values():
                if step["state"] == "done":
                    finished.append({"step": step["step"], "seconds": step["seconds"], **step["detail"]})
            return finished

    def snapshot(self) -> dict:
        """Make a plain copy of the whole state for /status.json, so the page never sees half-updated numbers.

        Returns:
            A dict with the steps, the crawl counters and whether the run is finished or failed.
        """
        with self.lock:
            crawl = self.crawl
            crawl_copy = {
                "total": crawl["total"],
                "done": crawl["done"],
                "notices": crawl["notices"],
                "status": dict(crawl["status"]),
                "active": sorted(crawl["active"]),
                "recent": list(crawl["recent"]),
            }
            return {
                "run_id": self.run_id,
                "now": time.time(),
                "finished": self.finished,
                "error": self.error,
                "steps": [dict(step) for step in self.steps.values()],
                "crawl": crawl_copy,
            }


class _Handler(SimpleHTTPRequestHandler):
    """Web server handler for the live page: serves the page, /status.json and the files of the run
    folder (report.html, raw pages).
    """
    def __init__(self, *args, progress: Progress, **kwargs):
        """
        Args:
            progress: the run's progress tracker, read on every /status.json request.
            *args, **kwargs: passed on to SimpleHTTPRequestHandler (request, address, server, directory).
        """
        self.progress = progress  # must be set first: the parent class handles the request inside __init__
        super().__init__(*args, **kwargs)

    def do_GET(self):
        """Answer a GET request: "/" is the live page, "/status.json" the current state, anything else
        a file from the run folder.
        """
        if self.path in ("/", "/index.html"):
            return self._send(LIVE_PAGE.encode(), "text/html; charset=utf-8")
        if self.path.startswith("/status.json"):
            return self._send(json.dumps(self.progress.snapshot()).encode(), "application/json")
        return super().do_GET()  # anything else is a file from the run folder

    def guess_type(self, path):
        """Saved town pages are sent as plain text, so whatever scripts they contain never run here.

        Args:
            path: the file being served.

        Returns:
            The Content-Type to send.
        """
        # saved town pages are shown as plain text, so whatever scripts they contain never run here
        return "text/plain; charset=utf-8" if "/raw/" in str(path) else super().guess_type(path)

    def _send(self, body: bytes, content_type: str):
        """Send a complete answer that the browser must not cache.

        Args:
            body: the content.
            content_type: its Content-Type.
        """
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Stay quiet; one line per request would drown the real log."""
        pass  # one line per request would drown the real log


def serve(progress: Progress, run_dir: Path, port: int) -> str:
    """Start the live page server in the background. It only listens on 127.0.0.1.

    Args:
        progress: the run's progress tracker, read on every /status.json request.
        run_dir: this run's folder, so report.html and the raw pages can be opened.
        port: the port to use; if it is taken, any free port is used instead.

    Returns:
        The address to open, e.g. "http://127.0.0.1:8765/".
    """
    def make_handler(*args, **kwargs):
        # the server creates one handler per request; each one needs the progress and the run folder
        return _Handler(*args, progress=progress, directory=str(run_dir), **kwargs)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler)
    except OSError:  # port already taken, let the system pick a free one
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler)
        log.warning("port %d is already in use (is another --live run still open?), using port %d instead",
                    port, server.server_port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/"
