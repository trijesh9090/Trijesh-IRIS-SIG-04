import json
import shutil
from pathlib import Path

from harvester.html_report import utm32_to_wgs84, write

FIX = Path(__file__).parent / "fixtures"


def test_utm32_to_wgs84_matches_postgis():
    # reference values from PostGIS ST_Transform(..., 4326)
    for (x, y), (lon, lat) in [((709762.6, 5412993.5), (11.858557, 48.834451)),
                               ((880000, 5300000), (14.069478, 47.741254))]:
        got = utm32_to_wgs84(x, y)
        assert abs(got[0] - lon) < 1e-5 and abs(got[1] - lat) < 1e-5, got


def test_report_html_has_all_towns_and_cannot_be_broken_by_scraped_text(tmp_path):
    (tmp_path / "raw").mkdir()
    shutil.copy(FIX / "gemeinden.json", tmp_path / "raw" / "gemeinden.json")
    (tmp_path / "raw" / "09273111_p1.html").write_text("<html></html>")
    outcomes = [{"ags": "09273111", "municipality": "Abensberg", "url": "https://www.abensberg.de/x", "link_type": "ongoing",
                 "status": "ok", "reason": None, "pages": 1, "notices": 1, "filtered": 3, "seconds": 2.1},
                {"ags": "09273112", "municipality": "Bad Abbach", "url": "https://bad-abbach.de/", "link_type": "ongoing",
                 "status": "error", "reason": "http_403", "pages": 0, "notices": 0, "filtered": 0, "seconds": 0.4}]
    report = {"run_id": "T1", "source_verified_at": "file", "filters": {}, "municipalities": 2, "records": 1,
              "status": {"ok": 1, "error": 1}, "reasons": {"http_403": 1}, "load": None, "outcomes": outcomes,
              "changes": {"new": ["09273111:abc"], "changed": []},
              "steps": [{"step": "crawl", "seconds": 3.0, "pages": 1}]}
    (tmp_path / "report.json").write_text(json.dumps(report))
    evil = "Bebauungsplan <!--<script> </script><script>alert(1)</script>"  # both ways to break a <script> block
    (tmp_path / "records.jsonl").write_text(json.dumps({
        "ags": "09273111", "source_record_id": "09273111:abc", "title": evil, "published_at": "2026-10-26", "signal_type": "bplan",
        "procedure_step": None, "confidence": "high", "notice_url": "https://www.abensberg.de/a.pdf"}) + "\n")

    html = write(tmp_path).read_text()
    block = html.split('type="application/json">')[1].split("</script>")[0]
    assert "<" not in block  # nothing in the data can open or close a tag, comment or script
    data = json.loads(block)  # and it still reads back as the original text
    assert html.count("</script>") == 3  # data block + leaflet + page script
    assert [t["municipality"] for t in data["towns"]] == ["Abensberg", "Bad Abbach"]
    abensberg = data["towns"][0]
    assert abensberg["raw"] == "09273111_p1.html" and abensberg["region"] == "Niederbayern"
    assert abs(abensberg["lonlat"][0] - 11.858557) < 1e-5  # portal midpoint converted for the map
    assert data["records"] == [["09273111", evil, "2026-10-26", "bplan", None, "high", "https://www.abensberg.de/a.pdf", "new"]]
    assert data["meta"]["steps"][0]["step"] == "crawl" and data["meta"]["tracked_changes"]
