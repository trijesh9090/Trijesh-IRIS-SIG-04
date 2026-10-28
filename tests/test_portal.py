from pathlib import Path

import pytest

from harvester import portal

FIX = Path(__file__).parent / "fixtures"


def u(ziel, url="https://g.bayern/p", defekt=False):
    return {"ziel": ziel, "url": url, "defekt": defekt}


@pytest.mark.parametrize("urls,expected", [
    ([u("url_1", "https://g/1"), u("url_2", "https://g/2")], ("https://g/2", "ongoing", None)),
    ([u("url_1", "https://g/1"), u("url_2", "https://g/2", defekt=True)], ("https://g/1", "general", None)),
    ([u("url_3")], (None, None, "only_url_3")),
    ([], (None, None, "no_link")),
    (None, (None, None, "no_link")),
    ([u("url_2", "https://x.de/sessionnet/bi")], ("https://x.de/sessionnet/bi", "ongoing", "council_system")),
    ([u("url_1", "https://geoportal.bayern.de/bayernatlas/?x")], ("https://geoportal.bayern.de/bayernatlas/?x", "general", "map_viewer")),
    ([u("url_2", "www.no-scheme.de")], ("www.no-scheme.de", "ongoing", "invalid_url")),
])
def test_pick_link(urls, expected):
    assert portal.pick_link(urls) == expected


def test_valid_point():
    assert portal.validate_point([709762.6, 5412993.5], [704941.75, 5406403.42, 714583.45, 5419583.59]) == \
        ((709762.6, 5412993.5), None)


@pytest.mark.parametrize("mid,bbox,problem", [
    ([10.24, 47.87], None, "coordinates_outside_bavaria_25832"),  # WGS84 degrees
    ([709762.6, 5412993.5], [0, 0, 1, 1], "midpoint_outside_boundingbox"),
    (None, None, "missing_coordinates"),
    (["abc", 1], None, "missing_coordinates"),
])
def test_invalid_points_are_rejected(mid, bbox, problem):
    assert portal.validate_point(mid, bbox) == (None, problem)


def test_entry_validation_and_filters():
    data = [
        {"name": "A", "ags": "09273111", "bezirk": "Niederbayern", "midpoint": [709762.6, 5412993.5],
         "bauleit_urls": [u("url_2")]},
        {"name": "B", "ags": "08111000", "bezirk": "Niederbayern", "bauleit_urls": [u("url_2")]},  # not Bavaria
        {"name": "C", "ags": "09100000", "bezirk": "Oberbayern"},
        "garbage",
    ]
    ms = portal.municipalities(data, region="niederbayern")
    assert [(m.name, m.skip_reason) for m in ms] == [("A", None), ("B", "invalid_ags"), ("", "malformed_entry")]


def test_empty_portal_is_an_error():
    with pytest.raises(ValueError):
        portal.load(b"[]")


def test_saved_portal_snapshot():
    """The live portal is replaced on 31.10.2026 by DiPlan; the snapshot keeps the pipeline demonstrable."""
    data = portal.load((FIX / "gemeinden.json").read_bytes())
    ms = portal.municipalities(data)
    assert len(ms) == 2056
    usable = [m for m in ms if not m.skip_reason]
    assert len(usable) > 1700
    assert all(m.point for m in usable)
