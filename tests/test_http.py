"""Fetcher against a local stdlib HTTP server: retries, 429, robots.txt rules, error reason codes."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harvester.http import Fetcher, FetchError, RobotRules

UA = "iris-test/0.1 (+mailto:test@example.com)"


@pytest.fixture
def server():
    routes, hits = {}, []  # path -> list of (status, headers, body); last entry repeats

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, self.headers.get("User-Agent")))
            seq = routes.get(self.path, [(404, {}, b"not found")])
            status, headers, body = seq.pop(0) if len(seq) > 1 else seq[0]
            if body == "SLEEP":
                time.sleep(1.5)
                body = b"late"
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", routes, hits
    srv.shutdown()


def fetcher(**kw):
    return Fetcher(UA, **{"delay": 0, "timeout": 5, "backoff": 0, **kw})


HTML = (200, {"Content-Type": "text/html"}, b"<html>ok</html>")


def test_retries_5xx_then_succeeds(server):
    base, routes, hits = server
    routes["/p"] = [(503, {}, b"busy"), (503, {}, b"busy"), HTML]
    assert fetcher().get(base + "/p").content == b"<html>ok</html>"
    assert [p for p, _ in hits].count("/p") == 3
    assert all(ua == UA for _, ua in hits)  # honest User-Agent on every request


def test_429_honours_retry_after(server):
    base, routes, _ = server
    routes["/p"] = [(429, {"Retry-After": "1"}, b"slow down"), HTML]
    t = time.monotonic()
    assert fetcher().get(base + "/p").status_code == 200
    assert time.monotonic() - t >= 0.9


def test_gives_up_with_reason_code(server):
    base, routes, _ = server
    routes["/p"] = [(500, {}, b"err")]
    with pytest.raises(FetchError) as e:
        fetcher(retries=1).get(base + "/p")
    assert e.value.reason == "http_500"
    with pytest.raises(FetchError) as e:
        fetcher().get(base + "/missing")
    assert e.value.reason == "http_404"


def test_empty_body_and_timeout(server):
    base, routes, _ = server
    routes["/empty"] = [(200, {}, b"  \n")]
    routes["/slow"] = [(200, {}, "SLEEP")]
    with pytest.raises(FetchError) as e:
        fetcher().get(base + "/empty")
    assert e.value.reason == "empty_response"
    with pytest.raises(FetchError) as e:
        fetcher(timeout=0.5, retries=0).get(base + "/slow")
    assert e.value.reason == "timeout"


def test_robots_disallow_is_respected(server):
    base, routes, hits = server
    routes["/robots.txt"] = [(200, {}, b"User-agent: *\nDisallow: /private\n")]
    routes["/private/x"] = [HTML]
    routes["/public"] = [HTML]
    f = fetcher()
    with pytest.raises(FetchError) as e:
        f.get(base + "/private/x")
    assert e.value.reason == "robots_disallowed"
    assert "/private/x" not in [p for p, _ in hits]  # never requested
    assert f.get(base + "/public").status_code == 200


def test_robots_missing_allows_but_robots_5xx_blocks(server):
    base, routes, _ = server
    routes["/p"] = [HTML]
    assert fetcher().get(base + "/p").status_code == 200  # robots.txt 404 -> no rules
    routes["/robots.txt"] = [(500, {}, b"err")]
    with pytest.raises(FetchError) as e:
        fetcher(retries=0).get(base + "/p")
    assert e.value.reason == "robots_unreachable:http_500"  # RFC 9309: treat as full disallow, keep the cause


def test_per_host_delay(server):
    base, routes, _ = server
    routes["/p"] = [HTML]
    f = fetcher(delay=0.5)
    t = time.monotonic()
    f.get(base + "/p")  # robots.txt + /p
    f.get(base + "/p")
    assert time.monotonic() - t >= 1.0  # 3 requests to one host -> at least 2 waits


def test_per_host_delay_holds_across_threads(server):
    from concurrent.futures import ThreadPoolExecutor
    base, routes, _ = server
    routes["/p"] = [HTML]
    f = fetcher(delay=0.3)
    t = time.monotonic()
    with ThreadPoolExecutor(4) as pool:
        assert all(r.status_code == 200 for r in pool.map(lambda _: f.get(base + "/p"), range(4)))
    assert time.monotonic() - t >= 1.2  # robots.txt + 4 pages on one host -> 4 gaps, even with 4 workers


def test_too_large_and_ssl_errors_get_their_own_reason(server, monkeypatch):
    base, routes, _ = server
    monkeypatch.setattr("harvester.http.MAX_BYTES", 50)  # instead of sending 8 MB in a test
    routes["/big"] = [(200, {"Content-Type": "text/html"}, b"x" * 100)]
    with pytest.raises(FetchError) as e:
        fetcher().get(base + "/big")
    assert e.value.reason == "too_large"
    # talking https to a plain-http server fails the TLS handshake
    with pytest.raises(FetchError) as e:
        fetcher(retries=0)._raw_get(base.replace("http://", "https://") + "/p")
    assert e.value.reason == "ssl_error"


def test_robots_rules_follow_rfc_9309():
    """Longest match wins, Allow wins a tie, * and $ work, and our own group beats "*".
    Python's urllib.robotparser gets the first three wrong (first match wins, no wildcards)."""
    rules = RobotRules("User-agent: *\nAllow: /\nDisallow: /intern/\nDisallow: /*.pdf$\nDisallow: /*?seite=\n"
                       "Allow: /intern/oeffentlich\n", "iris-bauleitplanung-harvester")
    assert not rules.can_fetch("https://a.de/intern/plan.html")
    assert not rules.can_fetch("https://a.de/files/plan.pdf")
    assert rules.can_fetch("https://a.de/files/plan.pdf?x=1")  # $ means the path must end there
    assert not rules.can_fetch("https://a.de/bek?seite=2")
    assert rules.can_fetch("https://a.de/intern/oeffentlich/bplan")  # the longer Allow wins
    assert rules.can_fetch("https://a.de/bauleitplanung")
    own = RobotRules("User-agent: *\nDisallow: /\n\nUser-agent: Iris-Bauleitplanung-Harvester\nDisallow: /privat\n",
                     "iris-bauleitplanung-harvester")
    assert own.can_fetch("https://a.de/bauleitplanung") and not own.can_fetch("https://a.de/privat/x")
    assert RobotRules("", "iris").can_fetch("https://a.de/anything")  # no rules: allowed


def test_redirects_are_checked_step_by_step(server):
    """A redirect into a path robots.txt forbids is blocked; an allowed redirect ends on the new URL."""
    base, routes, hits = server
    routes["/robots.txt"] = [(200, {}, b"User-agent: *\nDisallow: /intern/\n")]
    routes["/a"] = [(302, {"Location": "/intern/x"}, b"")]
    routes["/b"] = [(301, {"Location": "/c"}, b"")]
    routes["/c"] = [HTML]
    routes["/loop"] = [(302, {"Location": "/loop"}, b"")]
    f = fetcher()
    with pytest.raises(FetchError) as e:
        f.get(base + "/a")
    assert e.value.reason == "robots_disallowed" and "/intern/x" not in [p for p, _ in hits]
    assert f.get(base + "/b").url == base + "/c"
    with pytest.raises(FetchError) as e:
        f.get(base + "/loop")
    assert e.value.reason == "too_many_redirects"


def test_retry_after_is_capped(server, monkeypatch):
    """A site asking us to come back in an hour must not block a worker for hours."""
    base, routes, _ = server
    monkeypatch.setattr("harvester.http.RETRY_AFTER_MAX", 1)
    routes["/p"] = [(503, {"Retry-After": "3600"}, b"busy"), HTML]
    t = time.monotonic()
    assert fetcher().get(base + "/p").status_code == 200
    assert time.monotonic() - t < 5
