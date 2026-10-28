"""Turns parsed notices into rows for iris_staging.signal_ingest: id, fingerprint, flags and confidence."""
import hashlib
import re
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import portal
from .parse import Notice

SOURCE = "bauleitplanungsportal_by"

# query parameters that change on every visit and must not change a record's id
VOLATILE = re.compile(r"(?i)^(expires|signature|key-pair-id|policy|x-amz-.*|x-goog-.*|refresh)$")


def sha(text: str) -> str:
    """Hash a text with SHA-256. Used for record ids and content fingerprints.

    Args:
        text: any text.

    Returns:
        The hash as a hex string.
    """
    return hashlib.sha256(text.encode()).hexdigest()


def stable(url: str) -> str:
    """Remove query parameters that change on every visit, e.g. Erbendorf's ?Expires=..&Signature=..
    or Niederaichbach's &refresh=<token>.

    Only used to build ids and hashes. The URL we store stays the original, working one.

    Args:
        url: a notice or document URL.

    Returns:
        The same URL without the changing parameters.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if not VOLATILE.match(name):
            kept.append((name, value))
    query = urlencode(kept)
    return urlunsplit(parts._replace(query=query))


def to_row(notice: Notice, town: portal.Municipality, page_url: str, run_id: str) -> dict:
    """Turn a parsed notice into a row for iris_staging.signal_ingest.

    Args:
        notice: the notice from the parser.
        town: the town it belongs to (gives ags, names and the point).
        page_url: the page we scraped it from.
        run_id: id of the current run.

    Returns:
        A dict with one key per table column, plus x and y for the point. The id and content_hash
        come out the same for the same notice on every run, which is what prevents duplicates.
    """
    # flags say what is missing or uncertain, so nothing gets hidden
    flags = list(notice.flags)
    if notice.published_at is None:
        flags.append("missing_date")
    elif notice.published_at > date.today():
        flags.append("date_in_future")
    if not notice.document_urls:
        flags.append("no_documents")
    if notice.procedure_step is None:
        flags.append("step_unknown")
    if town.link_type == "general":
        flags.append("fallback_url_1")
    if town.point is None:
        flags.append(town.geo_problem or "unresolved_location")
    # date and a link = high, a link only = medium, neither = low
    has_link = bool(notice.document_urls) or notice.notice_url != page_url
    if notice.published_at and has_link:
        confidence = "high"
    elif has_link:
        confidence = "medium"
    else:
        confidence = "low"
    if "direct_pdf" in flags:
        confidence = "low"
    # the town's point; no point means the location is unresolved
    x, y, geo_status = None, None, "unresolved"
    if town.point:
        x, y = town.point
        geo_status = "municipality_centroid"
    published = None
    if notice.published_at:
        published = notice.published_at.isoformat()
    return {
        "source": SOURCE,
        # must come out the same for the same notice on every run, that's what stops duplicates
        "source_record_id": f"{town.ags}:{sha(stable(notice.notice_url) + '|' + notice.title.lower())[:16]}",
        "signal_type": notice.signal_type,
        "procedure_step": notice.procedure_step,
        "title": notice.title,
        "published_at": published,
        "date_source": notice.date_source,
        "notice_url": notice.notice_url,
        "source_page_url": page_url,
        "link_type": town.link_type,
        "document_urls": notice.document_urls,
        "ags": town.ags,
        "municipality": town.name,
        "district": town.district,
        "region": town.region,
        "x": x,
        "y": y,
        "geo_status": geo_status,
        "parser": "generic",
        "confidence": confidence,
        "quality_flags": flags,
        # fingerprint of the parts that can change; a different hash means the notice was updated
        "content_hash": sha("|".join([notice.title, published or "", notice.procedure_step or "",
                                       *sorted(map(stable, notice.document_urls))])),
        "run_id": run_id,
    }


def dedupe(rows: list[dict]) -> tuple[list[dict], int]:
    """Drop notices that appear twice in this run (same id), keeping the first one.

    Args:
        rows: all records of the run.

    Returns:
        (unique rows, how many were dropped).
    """
    unique = {}
    for row in rows:
        if row["source_record_id"] not in unique:
            unique[row["source_record_id"]] = row
    return list(unique.values()), len(rows) - len(unique)
