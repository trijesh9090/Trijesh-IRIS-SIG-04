"""End-to-end with a fake fetcher (no network) + idempotent load against PostGIS (needs DATABASE_URL)."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from harvester import portal
from harvester.pipeline import decode_page, harvest
from harvester.records import dedupe, stable, to_row
from harvester.http import FetchError

FIX = Path(__file__).parent / "fixtures"
ABENSBERG = {"name": "Abensberg", "ags": "09273111", "kreis": "Kelheim", "bezirk": "Niederbayern",
             "midpoint": [709762.6, 5412993.5], "boundingbox": [704941.75, 5406403.42, 714583.45, 5419583.59],
             "bauleit_urls": [{"ziel": "url_2", "url": "https://www.abensberg.de/buergerservice/bekanntmachungen",
                               "defekt": False}]}


class FakeFetcher:
    def __init__(self, pages: dict):
        self.pages = pages

    def get(self, url):
        v = self.pages[url]
        if isinstance(v, Exception):
            raise v
        ctype, body = v
        return SimpleNamespace(url=url, content=body, headers={"Content-Type": ctype})


def abensberg_rows(run_id="r1"):
    m = portal.parse_entry(ABENSBERG)
    f = FakeFetcher({m.url: ("text/html", (FIX / "abensberg_bekanntmachungen.html").read_bytes())})
    rows, outcome = harvest(f, m, run_id, max_pages=3, raw_dir=None)
    assert outcome["status"] == "ok"
    return rows


def test_rows_are_traceable_and_stable_across_runs():
    a, b = abensberg_rows("r1"), abensberg_rows("r2")
    assert len(a) == 1
    r = a[0]
    assert r["source_record_id"] == b[0]["source_record_id"] and r["content_hash"] == b[0]["content_hash"]
    assert r["source_record_id"].startswith("09273111:")
    assert (r["x"], r["y"], r["geo_status"]) == (709762.6, 5412993.5, "municipality_centroid")
    assert r["confidence"] == "high" and r["link_type"] == "ongoing"


def test_signed_cdn_urls_keep_record_id_stable():
    """Real case (Erbendorf): the PDF link's ?Expires=..&Signature=.. changes between runs."""
    from harvester.parse import parse_direct_pdf
    m = portal.parse_entry(ABENSBERG)
    base = "https://cdn.example.net/files/20260702_Bek_Bebauungsplan.pdf?v=2"
    a, b = (to_row(parse_direct_pdf(f"{base}&Expires={e}&Signature={s}&Key-Pair-Id=K2"), m, "p", "r")
            for e, s in (("1795724537", "cQew"), ("1795799999", "LKWT")))
    assert (a["source_record_id"], a["content_hash"]) == (b["source_record_id"], b["content_hash"])
    assert a["notice_url"] != b["notice_url"]  # the stored link stays the fresh, downloadable one
    assert stable(a["notice_url"]) == base
    assert stable("https://g.de/download/plan/?wpdmdl=16677&refresh=6ae118b3") == "https://g.de/download/plan/?wpdmdl=16677"


def test_network_failure_is_reported_not_raised():
    m = portal.parse_entry(ABENSBERG)
    rows, outcome = harvest(FakeFetcher({m.url: FetchError("timeout")}), m, "r", 3, None)
    assert rows == [] and (outcome["status"], outcome["reason"]) == ("error", "timeout")


def test_pagination_failure_keeps_first_page():
    m = portal.parse_entry(ABENSBERG)
    p1 = (FIX / "abensberg_bekanntmachungen.html").read_bytes().replace(
        b"</main>", b'<a rel="next" href="?page=2">weiter</a></main>')
    f = FakeFetcher({m.url: ("text/html", p1), m.url + "?page=2": FetchError("http_500")})
    rows, outcome = harvest(f, m, "r", 3, None)
    assert len(rows) == 1 and outcome["reason"] == "pagination_http_500"


def test_pagination_cap_is_reported_and_zero_means_no_cap():
    m = portal.parse_entry(ABENSBERG)
    body = (FIX / "abensberg_bekanntmachungen.html").read_bytes()
    pages = {m.url: ("text/html", body.replace(b"</main>", b'<a rel="next" href="?page=2">weiter</a></main>')),
             m.url + "?page=2": ("text/html", body.replace(b"</main>", b'<a rel="next" href="?page=3">weiter</a></main>')),
             m.url + "?page=3": ("text/html", body)}
    _, o = harvest(FakeFetcher(pages), m, "r", 2, None)
    assert (o["pages"], o["reason"]) == (2, "pagination_capped")
    _, o = harvest(FakeFetcher(pages), m, "r", 0, None)
    assert (o["pages"], o["reason"]) == (3, None)


def test_unexpected_error_in_one_town_is_reported_not_raised():
    """A bug or a very odd page (here: a URL urllib can't even split) must not stop the other towns."""
    m = portal.parse_entry(ABENSBERG)
    _, o = harvest(FakeFetcher({m.url: ValueError("Invalid IPv6 URL")}), m, "r", 3, None)
    assert (o["status"], o["reason"]) == ("error", "unexpected_error:ValueError")


def test_later_page_errors_keep_earlier_notices():
    m = portal.parse_entry(ABENSBERG)
    p1 = (FIX / "abensberg_bekanntmachungen.html").read_bytes().replace(
        b"</main>", b'<a rel="next" href="?page=2">weiter</a></main>')
    rows, o = harvest(FakeFetcher({m.url: ("text/html", p1), m.url + "?page=2": ("image/png", b"x")}), m, "r", 3, None)
    assert len(rows) == 1 and (o["status"], o["reason"], o["notices"]) == ("ok", "pagination_not_html", 1)


def test_decode_page_keeps_one_bad_byte_from_garbling_the_page():
    """Real cases: mainburg.de (UTF-8 with one broken byte), ungerhausen.de (UTF-8 sent as windows-1252)."""
    def resp(body, ctype):
        return SimpleNamespace(content=body, headers={"Content-Type": ctype})
    assert decode_page(resp("Bebauungsplan für Süd".encode(), "text/html; charset=windows-1252")) == "Bebauungsplan für Süd"
    broken = "für Süd".encode() + b"\xff"
    assert decode_page(resp(broken, "text/html; charset=utf-8")) == "für Süd\ufffd"
    latin1 = "für".encode("latin-1")
    assert decode_page(resp(latin1, "text/html")) == latin1  # not UTF-8 and not declared: BeautifulSoup decides


def test_non_html_and_empty_responses():
    m = portal.parse_entry(ABENSBERG)
    _, o = harvest(FakeFetcher({m.url: ("image/png", b"\x89PNG")}), m, "r", 3, None)
    assert o["reason"] == "not_html"
    _, o = harvest(FakeFetcher({m.url: FetchError("empty_response")}), m, "r", 3, None)
    assert o["reason"] == "empty_response"


def test_invalid_coordinates_give_unresolved_row():
    m = portal.parse_entry({**ABENSBERG, "midpoint": [11.87, 48.81]})  # degrees, not EPSG:25832
    f = FakeFetcher({m.url: ("text/html", (FIX / "abensberg_bekanntmachungen.html").read_bytes())})
    rows, _ = harvest(f, m, "r", 3, None)
    assert (rows[0]["x"], rows[0]["geo_status"]) == (None, "unresolved")
    assert "coordinates_outside_bavaria_25832" in rows[0]["quality_flags"]


def test_dedupe_within_run():
    rows = abensberg_rows() * 3
    out, dupes = dedupe(rows)
    assert (len(out), dupes) == (1, 2)


# --- database (rolled back after each test, so dev data is untouched) ---------------------------
@pytest.fixture
def conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set (docker compose up -d)")
    psycopg = pytest.importorskip("psycopg")
    from harvester.load import ensure_schema
    try:
        c = psycopg.connect(url, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database not reachable (start it with: docker compose up -d)")
    with c:
        with c.transaction(force_rollback=True):
            ensure_schema(c)
            c.execute("DELETE FROM iris_staging.signal_ingest")
            yield c


def count(conn):
    return conn.execute("SELECT count(*) FROM iris_staging.signal_ingest").fetchone()[0]


AGE_ROW = ("UPDATE iris_staging.signal_ingest SET first_seen_at = now() - interval '1 day', "
           "last_seen_at = now() - interval '1 day', updated_at = now() - interval '1 day'")
TIMES = ("SELECT first_seen_at < now() - interval '1 hour', last_seen_at = now(), updated_at = now() "
         "FROM iris_staging.signal_ingest")


def test_rerun_does_not_duplicate(conn):
    """Second run: same row, nothing new, first_seen_at kept, last_seen_at moved, updated_at kept (no change).
    The row is first made a day old, because inside one test transaction now() never changes."""
    from harvester.load import upsert
    s1 = upsert(conn, abensberg_rows("r1"))
    assert s1["inserted"] == 1 and s1["new_ids"] == [abensberg_rows()[0]["source_record_id"]]
    first_id = conn.execute("SELECT id FROM iris_staging.signal_ingest").fetchone()[0]
    conn.execute(AGE_ROW)
    s = upsert(conn, abensberg_rows("r2"))
    assert (s["inserted"], s["unchanged"], count(conn)) == (0, 1, 1)
    row = conn.execute("SELECT id, run_id, ST_SRID(geom), ST_X(geom) FROM iris_staging.signal_ingest").fetchone()
    assert row == (first_id, "r2", 25832, 709762.6)
    kept_first_seen, moved_last_seen, moved_updated = conn.execute(TIMES).fetchone()
    assert kept_first_seen and moved_last_seen and not moved_updated


def test_changed_content_updates_in_place(conn):
    """Changed content: same row, updated_at and last_seen_at move, first_seen_at stays."""
    from harvester.load import upsert
    upsert(conn, abensberg_rows("r1"))
    conn.execute(AGE_ROW)
    changed = [{**abensberg_rows("r2")[0], "procedure_step": "auslegung", "content_hash": "different"}]
    s2 = upsert(conn, changed)
    assert s2["updated_changed"] == 1 and s2["changed_ids"] == [changed[0]["source_record_id"]] and s2["new_ids"] == []
    assert count(conn) == 1
    kept_first_seen, moved_last_seen, moved_updated = conn.execute(TIMES).fetchone()
    assert kept_first_seen and moved_last_seen and moved_updated


def test_reclassification_counts_as_change(conn):
    from harvester.load import upsert
    upsert(conn, abensberg_rows("r1"))
    assert upsert(conn, [{**abensberg_rows("r2")[0], "signal_type": "fnp"}])["updated_changed"] == 1


def test_db_rejects_invalid_coordinates_visibly(conn):
    from harvester.load import upsert
    bad = [{**abensberg_rows()[0], "x": 11.87, "y": 48.81}]  # bypassing Python validation on purpose
    s = upsert(conn, bad)
    assert s["inserted"] == 0 and "signal_ingest_geom_bavaria" in s["rejected"][0]["error"]
    assert count(conn) == 0


def test_database_down_is_reported_not_a_crash(tmp_path, monkeypatch):
    """Store step fails -> run still writes records.jsonl, report.json and the dashboard, and exits 1."""
    import json
    from harvester.__main__ import main
    monkeypatch.setenv("HARVESTER_USER_AGENT", "iris-test/0.1 (+mailto:test@example.com)")
    monkeypatch.setenv("DATABASE_URL", "postgresql://iris:x@127.0.0.1:1/iris")  # nothing listens on port 1
    monkeypatch.setattr("harvester.pipeline.harvest", lambda f, m, run_id, *a: (abensberg_rows(run_id), {
        "ags": m.ags, "municipality": m.name, "url": m.url, "link_type": m.link_type, "status": "ok", "reason": None,
        "pages": 1, "notices": 1, "filtered": 0}))
    code = main(["--ags", "09273111", "--portal-file", str(FIX / "gemeinden.json"), "--out-dir", str(tmp_path)])
    run = next(tmp_path.iterdir())
    report = json.loads((run / "report.json").read_text())
    assert code == 1 and report["load"] is None and report["load_error"]
    assert (run / "records.jsonl").read_text().count("\n") == 1 and (run / "report.html").exists()


def test_portal_unreachable_or_unusable_stops_the_run(tmp_path, monkeypatch):
    """No municipality list means nothing to do: exit code 1, no records, nothing written to the database."""
    from harvester.__main__ import main
    from harvester.http import Fetcher
    monkeypatch.setenv("HARVESTER_USER_AGENT", "iris-test/0.1 (+mailto:test@example.com)")

    def portal_down(self, url):
        raise FetchError("timeout", url)
    monkeypatch.setattr(Fetcher, "get", portal_down)
    assert main(["--dry-run", "--out-dir", str(tmp_path / "a")]) == 1
    run = next((tmp_path / "a").iterdir())
    assert not (run / "records.jsonl").exists() and not (run / "report.json").exists()

    assert main(["--dry-run", "--portal-file", str(tmp_path / "missing.json"), "--out-dir", str(tmp_path / "c")]) == 1
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    assert main(["--dry-run", "--portal-file", str(empty), "--out-dir", str(tmp_path / "b")]) == 1


def test_missing_settings_stop_before_anything_runs(tmp_path, monkeypatch):
    """Missing User-Agent, or missing DATABASE_URL without --dry-run: exit code 2 and no run folder."""
    from harvester.__main__ import main
    monkeypatch.delenv("HARVESTER_USER_AGENT", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["--dry-run", "--out-dir", str(tmp_path)]) == 2
    monkeypatch.setenv("HARVESTER_USER_AGENT", "iris-test/0.1 (+mailto:test@example.com)")
    assert main(["--out-dir", str(tmp_path)]) == 2  # no DATABASE_URL and no --dry-run
    assert not tmp_path.exists() or not any(tmp_path.iterdir())
