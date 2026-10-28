"""Run progress state and the --live page server (localhost, stdlib)."""
import json
from urllib.request import urlopen

from harvester.live import Progress, serve


def test_progress_tracks_steps_and_towns():
    p = Progress("R1")
    p.begin("portal")
    p.end("portal", municipalities=2056)
    p.begin("crawl")
    p.crawl["total"] = 2
    p.town_started("Abensberg")
    assert p.snapshot()["crawl"]["active"] == ["Abensberg"]
    p.town_done({"municipality": "Abensberg", "status": "ok", "reason": None, "notices": 3})
    snap = p.snapshot()
    assert [s["state"] for s in snap["steps"]][:3] == ["done", "waiting", "running"]
    assert snap["crawl"]["done"] == 1 and snap["crawl"]["notices"] == 3 and snap["crawl"]["active"] == []
    assert snap["crawl"]["recent"][0]["municipality"] == "Abensberg"
    assert [t["step"] for t in p.timings()] == ["portal"] and p.timings()[0]["municipalities"] == 2056
    p.fail("crawl", "boom")
    assert p.snapshot()["error"] == "Extract notices: boom"
    json.dumps(p.snapshot())  # the live page needs plain JSON


def test_live_server_serves_page_status_and_run_files(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "09273111_p1.html").write_text("<script>alert(1)</script>")
    (tmp_path / "report.html").write_text("<html>dashboard</html>")
    p = Progress("R2")
    url = serve(p, tmp_path, 0)
    assert "Harvest run" in urlopen(url).read().decode()
    assert json.loads(urlopen(url + "status.json").read())["run_id"] == "R2"
    assert urlopen(url + "report.html").read() == b"<html>dashboard</html>"
    raw = urlopen(url + "raw/09273111_p1.html")
    assert raw.headers["Content-Type"].startswith("text/plain")  # saved municipal pages never run as HTML here
