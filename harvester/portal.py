"""Step 1 of the pipeline: read the portal's list of municipalities (gemeinden.json) and pick a page per town."""
import json
import re
from dataclasses import dataclass

PORTAL_URL = "https://geoportal.bayern.de/bauleitplanungsportal/data/gemeinden.json"

# Rough box around Bavaria in EPSG:25832 (metres). If someone hands us GPS degrees like 10.24 / 47.87,
# they land way outside this box and get rejected.
BAVARIA_X = (480_000, 900_000)
BAVARIA_Y = (5_230_000, 5_620_000)

# Council info systems and map viewers aren't notice pages we can read, so towns linking to them are skipped.
COUNCIL_RE = re.compile(r"sessionnet|buergerinfo|bürgerinfo|ratsinfo", re.I)
MAP_RE = re.compile(r"bayernatlas|geoportal\.bayern\.de/bauleitplanungsauskunft", re.I)


@dataclass
class Municipality:
    """One town from the portal list, already checked and ready to scrape (or marked to skip).

    Attributes:
        ags: official 8-digit municipality key, starts with 09 in Bavaria.
        name: town name.
        district: Landkreis.
        region: Regierungsbezirk.
        point: (x, y) in EPSG:25832 if the portal's midpoint looked right, else None.
        geo_problem: why point is None, e.g. "coordinates_outside_bavaria_25832".
        url: the page we will scrape.
        link_type: "ongoing" for url_2, "general" for url_1.
        skip_reason: why we can't scrape this town, or None if we can.
    """
    ags: str
    name: str
    district: str | None
    region: str | None
    point: tuple[float, float] | None  # checked EPSG:25832 (x, y), or None if it looked wrong
    geo_problem: str | None  # why point is None
    url: str | None
    link_type: str | None  # 'ongoing' for url_2, 'general' for url_1
    skip_reason: str | None  # filled in when we can't scrape this town


def load(raw: bytes) -> list[dict]:
    """Read the portal file (gemeinden.json) into a list.

    Args:
        raw: the file content as bytes.

    Returns:
        The list of municipality entries, as plain dicts.

    Raises:
        ValueError: if the file is empty or not a JSON list. The run stops then, because carrying on
            would quietly give us zero towns.
    """
    data = json.loads(raw)
    if not isinstance(data, list) or not data:
        raise ValueError("gemeinden.json is empty or not a JSON list")
    return data


def pick_link(urls) -> tuple[str | None, str | None, str | None]:
    """Pick the page to scrape for one town.

    url_2 (ongoing procedures) wins, otherwise url_1 (general planning page). url_3 only lists finished
    plans, so we never use it. Links the portal marks as broken ("defekt") are ignored.

    Args:
        urls: the town's "bauleit_urls" list from the portal.

    Returns:
        (url, link_type, skip_reason). skip_reason is None if the page can be scraped, otherwise a short
        code like "no_link", "only_url_3", "council_system" or "map_viewer".
    """
    # collect the links that are there and not marked broken: {"url_1": ..., "url_2": ..., "url_3": ...}
    working = {}
    for link in urls or []:
        if isinstance(link, dict) and link.get("url") and not link.get("defekt"):
            working[link.get("ziel")] = link.get("url")

    if "url_2" in working:
        url, link_type = working["url_2"], "ongoing"
    else:
        url, link_type = working.get("url_1"), "general"

    if not url:
        if "url_3" in working:
            return None, None, "only_url_3"
        return None, None, "no_link"
    if not url.lower().startswith(("http://", "https://")):
        return url, link_type, "invalid_url"
    if COUNCIL_RE.search(url):
        return url, link_type, "council_system"
    if MAP_RE.search(url):
        return url, link_type, "map_viewer"
    return url, link_type, None


def validate_point(midpoint, bbox) -> tuple[tuple[float, float] | None, str | None]:
    """Check the town's midpoint before we use it as the record's location.

    Args:
        midpoint: [x, y] from the portal, should be EPSG:25832 metres.
        bbox: [minx, miny, maxx, maxy] of the town, used as a second check.

    Returns:
        ((x, y), None) if the point is fine, or (None, reason) if it is missing, outside Bavaria
        (e.g. GPS degrees by mistake) or outside the town's own box.
    """
    try:
        x, y = (float(v) for v in midpoint)
    except (TypeError, ValueError):
        return None, "missing_coordinates"
    if not (BAVARIA_X[0] <= x <= BAVARIA_X[1] and BAVARIA_Y[0] <= y <= BAVARIA_Y[1]):
        return None, "coordinates_outside_bavaria_25832"
    try:
        minx, miny, maxx, maxy = (float(v) for v in bbox)
        if not (minx <= x <= maxx and miny <= y <= maxy):
            return None, "midpoint_outside_boundingbox"
    except (TypeError, ValueError):
        pass  # the bbox is only a second check; a midpoint that is already inside Bavaria is good enough
    return (x, y), None


def parse_entry(entry: dict) -> Municipality:
    """Turn one raw portal entry into a Municipality.

    Args:
        entry: one entry (dict) from gemeinden.json.

    Returns:
        A Municipality. Bad entries are not dropped; they come back with a skip_reason
        ("invalid_ags", "missing_name") so they show up in the report.
    """
    ags = str(entry.get("ags") or "")
    name = (entry.get("name") or "").strip()
    url, link_type, skip_reason = pick_link(entry.get("bauleit_urls"))
    if not re.fullmatch(r"09\d{6}", ags):  # every Bavarian AGS starts with 09
        skip_reason = "invalid_ags"
    elif not name:
        skip_reason = "missing_name"
    point, geo_problem = validate_point(entry.get("midpoint"), entry.get("boundingbox"))
    return Municipality(ags, name, entry.get("kreis"), entry.get("bezirk"), point, geo_problem, url, link_type,
                        skip_reason)


def municipalities(data: list, region=None, district=None, ags=None) -> list[Municipality]:
    """Build the list of towns for this run, with the command-line filters applied.

    Args:
        data: the full portal list.
        region: only keep this Regierungsbezirk (optional).
        district: only keep this Landkreis (optional).
        ags: only keep these municipality keys (optional).

    Returns:
        A list of Municipality objects. Entries that aren't even dicts are kept as "malformed_entry".
    """
    towns = []
    for entry in data:
        if not isinstance(entry, dict):  # keep broken entries so they show up in the report
            towns.append(Municipality("", "", None, None, None, None, None, None, "malformed_entry"))
            continue
        if region and (entry.get("bezirk") or "").lower() != region.lower():
            continue
        if district and (entry.get("kreis") or "").lower() != district.lower():
            continue
        if ags and entry.get("ags") not in ags:
            continue
        towns.append(parse_entry(entry))
    return towns
