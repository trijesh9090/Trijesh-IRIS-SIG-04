"""Fetching pages politely: our real User-Agent, robots.txt, a pause between requests to the same site, retries."""
import logging
import re
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import ReadTimeoutError
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

MAX_BYTES = 8 * 1024 * 1024  # some sites link huge files; we stop reading after 8 MB
MAX_REDIRECTS = 5
RETRY_AFTER_MAX = 60  # seconds; a site asking us to come back in an hour would otherwise block a worker for hours
REDIRECT_CODES = (301, 302, 303, 307, 308)


class FetchError(Exception):
    """Something went wrong while fetching a page.

    `reason` is a short code (http_404, timeout, robots_disallowed, ...) that ends up in the run report.
    """

    def __init__(self, reason: str, detail: str = ""):
        """Args:
            reason: short code for the report.
            detail: extra text for the log, usually the URL or the original error.
        """
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class RobotRules:
    """The rules of one robots.txt, read the way RFC 9309 says.

    Python's own urllib.robotparser lets the first matching line win and ignores the * and $ wildcards,
    so "Allow: /" before "Disallow: /intern/" would let us into /intern/. Here the longest matching rule
    wins, an Allow wins a tie, and * and $ work.
    """

    def __init__(self, text: str, product_token: str):
        """Args:
            text: the content of robots.txt.
            product_token: our crawler's name, e.g. "iris-bauleitplanung-harvester". Rules for this name are
                used; if there are none, the rules for "*" are used.
        """
        groups = []  # (user agents, rules); rules are (pattern, allowed)
        last_was_agent = False
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, value = line.split(":", 1)
            field, value = field.strip().lower(), value.strip()
            if field == "user-agent":
                if not last_was_agent:  # a new group starts; several user-agent lines in a row share one
                    groups.append(([], []))
                groups[-1][0].append(value.lower())
                last_was_agent = True
            elif field in ("allow", "disallow") and groups:
                if value:  # an empty Disallow means "nothing is disallowed"
                    groups[-1][1].append((value, field == "allow"))
                last_was_agent = False
            else:
                last_was_agent = False

        token = product_token.lower()
        ours = [rule for agents, rules in groups if token in agents for rule in rules]
        anyone = [rule for agents, rules in groups if "*" in agents for rule in rules]
        has_own_group = any(token in agents for agents, _ in groups)
        self.rules = ours if has_own_group else anyone

    def can_fetch(self, url: str) -> bool:
        """Check if robots.txt lets us fetch a URL.

        Args:
            url: the full URL.

        Returns:
            True if allowed. The longest matching rule decides; with no matching rule, it is allowed.
        """
        parts = urlsplit(url)
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        if path == "/robots.txt":
            return True
        best_length, allowed = -1, True
        for pattern, is_allow in self.rules:
            if _robots_pattern(pattern).match(path):
                length = len(pattern)
                if length > best_length or (length == best_length and is_allow):
                    best_length, allowed = length, is_allow
        return allowed


def _robots_pattern(pattern: str) -> re.Pattern:
    """Turn a robots.txt path like "/*.pdf$" into a regex: * means anything, a $ at the end means "ends here".

    Args:
        pattern: the path from an Allow or Disallow line.

    Returns:
        A compiled regex to match against the start of a path.
    """
    ends_here = pattern.endswith("$")
    if ends_here:
        pattern = pattern[:-1]
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile(regex + ("$" if ends_here else ""))


class Fetcher:
    """Fetches web pages politely: real User-Agent, robots.txt, a pause between requests to the same
    site, and retries. Safe to use from several threads at once.
    """
    def __init__(self, user_agent: str, delay: float = 2.0, timeout: float = 20.0, retries: int = 3,
                 backoff: float = 1.0):
        """Args:
            user_agent: our User-Agent with a contact address, so site owners know who we are.
            delay: seconds to wait between two requests to the same site.
            timeout: seconds before we give up on a slow request.
            retries: how often to retry on 429 and 5xx answers.
            backoff: how fast the wait grows between retries.
        """
        self.delay, self.timeout = delay, timeout
        self.product_token = user_agent.split("/", 1)[0].strip()  # "iris-bauleitplanung-harvester/0.1 (...)"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "de,en;q=0.5"})
        # retry rate limits (429) and server errors a few times, and wait as long as the site asks us to,
        # but never longer than RETRY_AFTER_MAX
        retry = Retry(total=retries, backoff_factor=backoff, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET",), respect_retry_after_header=True, raise_on_status=False,
                      retry_after_max=RETRY_AFTER_MAX)
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotRules | str] = {}  # rules, or the reason we may not crawl the site
        self._locks: dict[object, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, key) -> threading.Lock:
        """Get the lock for one site (or one robots.txt), creating it the first time.

        Args:
            key: the site name, or ("robots", base_url).

        Returns:
            The threading.Lock for that key.
        """
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _raw_get(self, url: str, follow_redirects: bool = False) -> requests.Response:
        """Fetch a URL without the robots.txt check, keeping to one request per site at a time.

        The lock is held during the request, so requests to the same site are `delay` seconds apart.
        Requests to different sites can run in parallel.

        Args:
            url: the page to fetch.
            follow_redirects: let requests follow redirects by itself. Only used for robots.txt; pages are
                redirected step by step in get(), so every step is checked and paced.

        Returns:
            The response, with the body already read.

        Raises:
            FetchError: on timeout, network or SSL problems, or a body over 8 MB.
        """
        host = urlsplit(url).netloc.lower()
        with self._lock(host):
            wait = self._last.get(host, 0) + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()
            return self._request(url, follow_redirects)

    def _request(self, url: str, follow_redirects: bool = False) -> requests.Response:
        """Do the actual HTTP GET and turn every kind of failure into a FetchError.

        The body is streamed, so a huge file is cut off at MAX_BYTES instead of being loaded completely.

        Args:
            url: the page to fetch.
            follow_redirects: let requests follow redirects by itself.

        Returns:
            The response, with the body stored in resp.content.

        Raises:
            FetchError: "timeout", "ssl_error", "network_error" or "too_large".
        """
        try:
            with self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=follow_redirects) as resp:
                body = resp.raw.read(MAX_BYTES + 1, decode_content=True)
        except requests.exceptions.Timeout as e:
            raise FetchError("timeout", str(e)) from e
        except requests.exceptions.SSLError as e:
            raise FetchError("ssl_error", str(e)) from e
        except requests.exceptions.RequestException as e:
            # once the retries are used up, a read timeout comes back wrapped in a ConnectionError
            reason = None
            if e.args:
                reason = getattr(e.args[0], "reason", None)
            if isinstance(reason, ReadTimeoutError):
                raise FetchError("timeout", str(e)) from e
            raise FetchError("network_error", str(e)) from e
        except Exception as e:  # broken responses from urllib3 (bad encoding, cut-off body) end up here
            raise FetchError("network_error", repr(e)) from e
        if len(body) > MAX_BYTES:
            raise FetchError("too_large", url)
        resp._content = body
        return resp

    def _robots_for(self, url: str) -> RobotRules | str:
        """Get the robots.txt rules for a site, fetching them only once per site.

        As RFC 9309 says: a 4xx answer means there are no rules, a 5xx or no answer at all means
        we are not allowed in. Redirects of robots.txt are followed.

        Args:
            url: any URL on the site.

        Returns:
            The rules, or a reason like "robots_unreachable:timeout" if we must not crawl the site.
        """
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        with self._lock(("robots", base)):  # several workers may hit the same site at once
            if base not in self._robots:
                try:
                    resp = self._raw_get(base + "/robots.txt", follow_redirects=True)
                    if resp.status_code >= 500:
                        self._robots[base] = f"robots_unreachable:http_{resp.status_code}"
                    elif resp.status_code >= 400:
                        self._robots[base] = RobotRules("", self.product_token)  # no robots.txt: no rules
                    else:
                        self._robots[base] = RobotRules(resp.text, self.product_token)
                except FetchError as e:
                    self._robots[base] = f"robots_unreachable:{e.reason}"
            return self._robots[base]

    def _check_robots(self, url: str):
        """Raise if robots.txt doesn't let us fetch this URL.

        Args:
            url: the page we want.

        Raises:
            FetchError: "robots_unreachable:<why>" or "robots_disallowed".
        """
        rules = self._robots_for(url)
        if isinstance(rules, str):
            raise FetchError(rules, url)
        if not rules.can_fetch(url):
            raise FetchError("robots_disallowed", url)

    def get(self, url: str) -> requests.Response:
        """Fetch a page the polite way. This is the one to use from outside.

        Redirects are followed one step at a time (at most MAX_REDIRECTS), and every step goes through the
        robots.txt check and the pause for its own site.

        Args:
            url: the page to fetch.

        Returns:
            The response for a good page (status below 400, body not empty). resp.url is where we ended up.

        Raises:
            FetchError: "robots_unreachable:<why>", "robots_disallowed", "too_many_redirects", "http_<status>",
                "empty_response", or anything _request() raises.
        """
        for _ in range(MAX_REDIRECTS + 1):
            self._check_robots(url)
            resp = self._raw_get(url)
            location = resp.headers.get("Location")
            if resp.status_code in REDIRECT_CODES and location:
                url = urljoin(url, location)
                continue
            break
        else:
            raise FetchError("too_many_redirects", url)
        if resp.status_code >= 400:
            raise FetchError(f"http_{resp.status_code}", url)
        if not resp.content.strip():  # a 200 with nothing in it is no use to us either
            raise FetchError("empty_response", url)
        return resp
