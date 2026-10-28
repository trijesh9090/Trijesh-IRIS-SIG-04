"""The pipeline itself: the six steps of a run, and how one town is scraped.

    1. load municipalities   portal list (or a saved copy)
    2. select towns          --region / --district / --ags / --limit
    3. extract notices       scrape every town, several at a time
    4. remove duplicates
    5. store in database     idempotent upsert
    6. build dashboard       report.json + report.html
"""
import argparse
import json
import logging
import threading
import time
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import html_report, live, portal
from .http import Fetcher, FetchError
from .parse import is_pdf, parse_direct_pdf, parse_page
from .records import dedupe, to_row

log = logging.getLogger("harvester")


def harvest(fetcher: Fetcher, town: portal.Municipality, run_id: str, max_pages: int, raw_dir: Path | None):
    """Scrape one town, following its "next page" links.

    Never raises: a network or parse problem becomes the outcome's status and reason, so one bad site
    can't stop the run.

    Args:
        fetcher: the shared Fetcher.
        town: the town to scrape.
        run_id: id of the current run, stored on every record.
        max_pages: how many pages to follow at most; 0 means no limit.
        raw_dir: folder to save the raw pages in, or None to not save them.

    Returns:
        (rows, outcome). rows are the town's records; outcome says how it went, e.g.
        {"status": "ok" / "no_planning_content" / "error" / "skipped", "reason": ..., "pages": ...,
        "notices": ..., "filtered": ...}.
    """
    outcome = {"ags": town.ags, "municipality": town.name, "url": town.url, "link_type": town.link_type,
               "status": "skipped", "reason": town.skip_reason, "pages": 0, "notices": 0, "filtered": 0}
    if town.skip_reason:
        return [], outcome
    try:
        return _harvest_pages(fetcher, town, run_id, max_pages, raw_dir, outcome)
    except Exception as e:  # a bug or a very unusual page must not stop the other 2,000 towns
        log.exception("unexpected error for %s (%s)", town.name, town.url)
        outcome.update(status="error", reason=f"unexpected_error:{type(e).__name__}", notices=0)
        return [], outcome


def _harvest_pages(fetcher: Fetcher, town: portal.Municipality, run_id: str, max_pages: int, raw_dir: Path | None,
                   outcome: dict):
    """Fetch and parse the town's page and its "next" pages. Used by harvest(), which catches anything unexpected.

    A problem on the first page fails the town. A problem on a later page only stops the paging: the notices
    from the earlier pages are kept and the reason starts with "pagination_".

    Args:
        fetcher: the shared Fetcher.
        town: the town to scrape.
        run_id: id of the current run.
        max_pages: how many pages to follow at most; 0 means no limit.
        raw_dir: folder to save the raw pages in, or None.
        outcome: the town's outcome dict, filled in here.

    Returns:
        (rows, outcome), like harvest().
    """
    rows, url, visited = [], town.url, set()
    while url and url not in visited and (not max_pages or len(visited) < max_pages):
        visited.add(url)
        first_page = outcome["pages"] == 0
        try:
            resp = fetcher.get(url)
        except FetchError as e:
            if first_page:
                outcome.update(status="error", reason=e.reason)
                return [], outcome
            outcome["reason"] = f"pagination_{e.reason}"  # a later page failed: keep what we already have
            break
        outcome["pages"] += 1
        content_type = resp.headers.get("Content-Type", "").lower()
        if "pdf" in content_type or is_pdf(resp.url):  # the town links straight to a PDF
            rows.append(to_row(parse_direct_pdf(resp.url), town, resp.url, run_id))
            break
        if content_type and "html" not in content_type and "xml" not in content_type:
            if first_page:
                outcome.update(status="error", reason="not_html")
                return [], outcome
            outcome["reason"] = "pagination_not_html"
            break
        if raw_dir:  # keep the page as evidence
            (raw_dir / f"{town.ags}_p{outcome['pages']}.html").write_bytes(resp.content)
        try:
            page = parse_page(decode_page(resp), resp.url)
        except Exception as e:  # HTML so broken that even the parser gives up
            log.exception("parse failed %s", resp.url)
            if first_page:
                outcome.update(status="error", reason=f"parse_error:{type(e).__name__}")
                return [], outcome
            outcome["reason"] = f"pagination_parse_error:{type(e).__name__}"
            break
        outcome["filtered"] += page.filtered
        if page.status == "empty_or_js_page" and first_page:
            outcome.update(status="skipped", reason="empty_or_js_page")
            return [], outcome
        for notice in page.notices:
            rows.append(to_row(notice, town, resp.url, run_id))
        url = page.next_url
    if url and url not in visited and outcome["reason"] is None:
        outcome["reason"] = "pagination_capped"  # there were more pages than --max-pages allows
    outcome["notices"] = len(rows)
    outcome["status"] = "ok" if rows else "no_planning_content"
    return rows, outcome


def decode_page(resp) -> str | bytes:
    """Turn a page's bytes into text, so a single bad byte can't garble the whole page.

    Valid UTF-8 is used as UTF-8, whatever the server claims (some sites send UTF-8 but declare windows-1252).
    A page that says it is UTF-8 but has a broken byte is decoded with a replacement character for that byte.
    Anything else is left to BeautifulSoup, which reads the page's own charset or guesses.

    Args:
        resp: the HTTP response.

    Returns:
        The page as text, or the raw bytes if BeautifulSoup should work out the encoding.
    """
    body = resp.content
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        pass
    declared = (resp.headers.get("Content-Type", "") + " " + body[:2048].decode("ascii", "ignore")).lower()
    if "utf-8" in declared or "utf8" in declared:
        return body.decode("utf-8", errors="replace")
    return body


def run(args: argparse.Namespace, user_agent: str, db_url: str | None) -> int:
    """Run all six steps: load municipalities, select towns, extract notices, remove duplicates,
    store in the database, build the dashboard.

    Args:
        args: the command-line options from __main__.
        user_agent: our User-Agent with a contact address.
        db_url: database connection string; not needed with --dry-run.

    Returns:
        The exit code: 0 ok, 1 if the portal or the database step failed.
    """
    # every run gets its own folder: raw pages, records.jsonl, report.json and report.html
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.out_dir / run_id
    suffix = 1
    while run_dir.exists():  # two runs started in the same second must not share a folder
        suffix += 1
        run_dir = args.out_dir / f"{run_id}-{suffix}"
    run_id = run_dir.name
    (run_dir / "raw").mkdir(parents=True)
    fetcher = Fetcher(user_agent, delay=args.delay, timeout=args.timeout)
    progress = live.Progress(run_id)  # feeds the log, the step timings and the --live page
    if args.live:
        url = live.serve(progress, run_dir, args.port)
        log.info("live view: %s", url)
        webbrowser.open(url)

    portal_data = _load_portal(args, fetcher, run_dir, progress)
    if portal_data is None:
        return _linger(args.live, 1)
    data, verified = portal_data
    targets = _select(args, data, progress)
    results = _crawl(args, fetcher, targets, run_id, run_dir, progress)
    outcomes = [outcome for _, outcome in results]

    progress.begin("dedupe")
    all_rows = []
    for town_rows, _ in results:
        all_rows.extend(town_rows)
    rows, dupes = dedupe(all_rows)
    progress.end("dedupe", records=len(rows), duplicates=dupes)

    # written before the database step, so the scraped data is safe even if the load fails
    with open(run_dir / "records.jsonl", "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    load_stats, changes, load_error = _store(args, db_url, rows, run_dir, progress)

    progress.begin("report")
    report = {
        "run_id": run_id, "source_verified_at": verified,
        "filters": {"region": args.region, "district": args.district, "ags": args.ags, "limit": args.limit,
                    "max_pages": args.max_pages},
        "municipalities": len(targets),
        "status": Counter(outcome["status"] for outcome in outcomes),
        "reasons": Counter(outcome["reason"] for outcome in outcomes if outcome["reason"]),
        "records": len(rows), "duplicates_in_run": dupes,
        "signal_type": Counter(row["signal_type"] for row in rows),
        "confidence": Counter(row["confidence"] for row in rows),
        "quality_flags": Counter(flag for row in rows for flag in row["quality_flags"]),
        "load": load_stats, "load_error": load_error, "changes": changes, "steps": progress.timings(),
        "outcomes": outcomes,
    }
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html = html_report.write(run_dir)
    progress.end("report", file="report.html")
    progress.finished = True
    log.info("run %s done: %d records, report at %s and %s", run_id, len(rows), run_dir / "report.json", html)
    return _linger(args.live, 1 if load_error else 0)


def _load_portal(args, fetcher: Fetcher, run_dir: Path, progress: live.Progress):
    """Step 1: get the municipality list, from the portal or from a saved copy (--portal-file).

    Args:
        args: the command-line options.
        fetcher: the shared Fetcher.
        run_dir: this run's folder; a copy of the list is saved in raw/.
        progress: the run's progress tracker.

    Returns:
        (data, verified), where verified is the portal's Last-Modified header (or the file name).
        None if the portal can't be reached or the file is unusable; the run stops then.
    """
    progress.begin("portal")
    if args.portal_file:
        try:
            raw = args.portal_file.read_bytes()
        except OSError as e:
            log.error("cannot read --portal-file: %s", e)
            progress.fail("portal", f"cannot read {args.portal_file}")
            return None
        verified = f"file:{args.portal_file}"
    else:
        try:
            resp = fetcher.get(portal.PORTAL_URL)
        except FetchError as e:
            log.error("portal unreachable (%s); retry later or pass --portal-file", e)
            progress.fail("portal", f"unreachable ({e.reason})")
            return None
        raw, verified = resp.content, resp.headers.get("Last-Modified", "unknown")
    (run_dir / "raw" / "gemeinden.json").write_bytes(raw)
    try:
        data = portal.load(raw)
    except ValueError as e:
        log.error("portal data unusable: %s", e)
        progress.fail("portal", f"unusable ({e})")
        return None
    log.info("portal: %d municipalities, source Last-Modified: %s", len(data), verified)
    progress.end("portal", municipalities=len(data), source_verified_at=verified)
    return data, verified


def _select(args, data: list, progress: live.Progress) -> list[portal.Municipality]:
    """Step 2: apply --region, --district, --ags and --limit.

    Args:
        args: the command-line options.
        data: the full portal list.
        progress: the run's progress tracker.

    Returns:
        The towns to scrape in this run.
    """
    progress.begin("select")
    targets = portal.municipalities(data, args.region, args.district, args.ags)[: args.limit]
    skipped = sum(1 for town in targets if town.skip_reason)
    progress.end("select", towns=len(targets), with_link=len(targets) - skipped, skipped=skipped)
    return targets


def _crawl(args, fetcher: Fetcher, targets: list, run_id: str, run_dir: Path, progress: live.Progress) -> list:
    """Step 3: scrape all towns, several at a time. The fetcher still keeps it to one request per site.

    Args:
        args: the command-line options (--workers, --max-pages).
        fetcher: the shared Fetcher.
        targets: the towns to scrape.
        run_id: id of the current run.
        run_dir: this run's folder; raw pages go to raw/.
        progress: the run's progress tracker.

    Returns:
        A list of (rows, outcome) per town, sorted by AGS so the order is the same on every run.
    """
    progress.begin("crawl", workers=args.workers)
    progress.crawl["total"] = len(targets)

    def timed(town):
        """Scrape one town and note how long it took; also tells the live page when it starts.

        Args:
            town: the town to scrape.

        Returns:
            (rows, outcome) from harvest(), with "seconds" added to the outcome.
        """
        progress.town_started(town.name)
        started = time.monotonic()
        town_rows, outcome = harvest(fetcher, town, run_id, args.max_pages, run_dir / "raw")
        outcome["seconds"] = round(time.monotonic() - started, 1)
        return town_rows, outcome

    results = []
    with ThreadPoolExecutor(max(1, args.workers)) as pool:
        futures = {pool.submit(timed, town): town for town in targets}
        try:
            for done_count, future in enumerate(as_completed(futures), 1):
                town = futures[future]
                town_rows, outcome = future.result()
                log.info("[%d/%d] %s %s: %s %s notices=%d", done_count, len(targets), town.ags, town.name,
                         outcome["status"], outcome["reason"] or "", outcome["notices"])
                progress.town_done(outcome)
                results.append((town_rows, outcome))
        except KeyboardInterrupt:
            # Ctrl+C: drop the towns that haven't started; the ones in progress finish their current request
            log.warning("interrupted, cancelling the towns that haven't started yet")
            pool.shutdown(wait=False, cancel_futures=True)
            raise

    # same order every run, whichever town finished first
    results.sort(key=lambda result: result[1]["ags"])
    outcomes = [outcome for _, outcome in results]
    progress.end("crawl", workers=args.workers,
                 pages=sum(outcome["pages"] for outcome in outcomes),
                 notices=sum(outcome["notices"] for outcome in outcomes),
                 status=dict(Counter(outcome["status"] for outcome in outcomes)))
    return results


def _store(args, db_url: str | None, rows: list[dict], run_dir: Path, progress: live.Progress):
    """Step 5: write the records to the database.

    Args:
        args: the command-line options (--dry-run).
        db_url: database connection string.
        rows: the records to store.
        run_dir: this run's folder, only used in the error message.
        progress: the run's progress tracker.

    Returns:
        (load_stats, changes, load_error). On a dry run all three are None. If the database fails,
        only load_error is set and nothing was written (it is one transaction).
    """
    progress.begin("load")
    if args.dry_run:
        progress.end("load", skipped="dry run")
        return None, None, None
    import psycopg  # only needed when we really write to the database
    from .load import ensure_schema, upsert
    try:
        with psycopg.connect(db_url, connect_timeout=10) as conn:
            ensure_schema(conn)
            stats = upsert(conn, rows)  # one transaction, so it's all rows or none
    except psycopg.Error as e:
        # database down, wrong password, schema problem: nothing was written, but records.jsonl has everything
        error = str(e).splitlines()[0]
        log.error("load failed, nothing stored (records kept in %s): %s", run_dir / "records.jsonl", error)
        progress.fail("load", error)
        return None, None, error
    changes = {"new": stats.pop("new_ids"), "changed": stats.pop("changed_ids")}
    summary = dict(stats)
    summary["rejected"] = len(stats["rejected"])  # the log only needs how many
    log.info("load: %s", summary)
    progress.end("load", **summary)
    return stats, changes, None


def _linger(is_live: bool, code: int) -> int:
    """With --live, keep the page and the dashboard online until Ctrl+C.

    Args:
        is_live: True if --live was given.
        code: the exit code to return afterwards.

    Returns:
        code, unchanged.
    """
    if is_live:
        log.info("live view stays open; press Ctrl+C to stop")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    return code
