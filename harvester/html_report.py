"""Builds the dashboard (report.html) for a run: headline numbers, new and changed notices, the latest notices,
problems, a map and the list of all towns. It's one file with the data inside; only the map needs internet.

    python -m harvester.html_report data/runs/<run_id>     # (re)build report.html for an existing run
"""
import json
import sys
from datetime import date
from importlib import resources
from math import cos, degrees, radians, sin, sqrt, tan
from pathlib import Path

from . import portal

# the page itself lives in templates/dashboard.html
PAGE = (resources.files(__package__) / "templates" / "dashboard.html").read_text(encoding="utf-8")


def utm32_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """Convert an EPSG:25832 point to longitude/latitude for the map, using the standard UTM formulas
    for the GRS80 ellipsoid. Only used for display; within Bavaria it is off by less than a metre.

    Args:
        x: easting in metres.
        y: northing in metres.

    Returns:
        (longitude, latitude) in degrees, rounded to 6 decimals.
    """
    # the short names are the usual symbols of the UTM formulas (a = earth radius, e2 = eccentricity squared, ...)
    a, f, k0 = 6378137.0, 1 / 298.257222101, 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    e1 = (1 - sqrt(1 - e2)) / (1 + sqrt(1 - e2))
    mu = y / k0 / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    p = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * sin(2 * mu) + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * sin(4 * mu)
         + 151 * e1 ** 3 / 96 * sin(6 * mu) + 1097 * e1 ** 4 / 512 * sin(8 * mu))
    n, t, c = a / sqrt(1 - e2 * sin(p) ** 2), tan(p) ** 2, ep2 * cos(p) ** 2
    r = a * (1 - e2) / (1 - e2 * sin(p) ** 2) ** 1.5
    d = (x - 500000.0) / (n * k0)
    lat = p - n * tan(p) / r * (d ** 2 / 2 - (5 + 3 * t + 10 * c - 4 * c ** 2 - 9 * ep2) * d ** 4 / 24
                                + (61 + 90 * t + 298 * c + 45 * t ** 2 - 252 * ep2 - 3 * c ** 2) * d ** 6 / 720)
    lon = radians(9) + (d - (1 + 2 * t + c) * d ** 3 / 6
                        + (5 - 2 * c + 28 * t - 3 * c ** 2 + 8 * ep2 + 24 * t ** 2) * d ** 5 / 120) / cos(p)
    return round(degrees(lon), 6), round(degrees(lat), 6)


def build(run_dir: Path) -> dict:
    """Collect everything the dashboard needs from a run folder.

    Args:
        run_dir: the run folder with report.json, records.jsonl and raw/gemeinden.json.

    Returns:
        {"meta": run summary, "towns": one entry per town with status and map point,
        "records": one short list per record}.
    """
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    # which records were new or changed in this run (empty for a dry run)
    changes = report.get("changes") or {}
    change_of = {}
    for record_id in changes.get("new", []):
        change_of[record_id] = "new"
    for record_id in changes.get("changed", []):
        change_of[record_id] = "changed"

    # one short list per record, to keep the file small:
    # [ags, title, published_at, type, step, confidence, notice_url, "new" / "changed" / ""]
    records = []
    if (run_dir / "records.jsonl").exists():
        for line in (run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            records.append([row["ags"], row["title"], row["published_at"], row["signal_type"], row["procedure_step"],
                            row["confidence"], row["notice_url"], change_of.get(row["source_record_id"], "")])
    points = {}  # the portal copy saved with the run gives us each town's point, district and region
    if (run_dir / "raw" / "gemeinden.json").exists():
        portal_data = portal.load((run_dir / "raw" / "gemeinden.json").read_bytes())
        for town in portal.municipalities(portal_data):
            lonlat = None
            if town.point:
                lonlat = utm32_to_wgs84(*town.point)
            points[town.ags] = (lonlat, town.district, town.region)
    towns = []
    for outcome in report["outcomes"]:
        lonlat, district, region = points.get(outcome["ags"], (None, "", ""))
        raw_page = run_dir / "raw" / f"{outcome['ags']}_p1.html"
        town = dict(outcome)
        town["lonlat"] = lonlat
        town["district"] = district
        town["region"] = region
        town["raw"] = raw_page.name if raw_page.exists() else None
        towns.append(town)
    meta = {}
    for key in ("run_id", "source_verified_at", "filters", "municipalities", "records", "status", "reasons", "load",
                "steps"):
        meta[key] = report.get(key)
    meta["tracked_changes"] = report.get("changes") is not None
    meta["load_error"] = report.get("load_error")
    meta["today"] = date.today().isoformat()
    return {"meta": meta, "towns": towns, "records": records}


def write(run_dir: Path) -> Path:
    """Build report.html for a run. Can also be used later to rebuild the dashboard of an old run.

    Args:
        run_dir: the run folder.

    Returns:
        The path of the written report.html.
    """
    # every "<" is written as \u003c (still valid JSON, read back as "<"), so scraped text like "</script>" or
    # "<!--<script>" can never end or confuse the data block
    data = json.dumps(build(run_dir), ensure_ascii=False).replace("<", "\\u003c")
    out = run_dir / "report.html"
    out.write_text(PAGE.replace("__DATA__", data), encoding="utf-8")
    return out


if __name__ == "__main__":
    for arg in sys.argv[1:] or sys.exit("usage: python -m harvester.html_report data/runs/<run_id> [...]"):
        print(write(Path(arg)))
