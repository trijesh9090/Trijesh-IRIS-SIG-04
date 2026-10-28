from datetime import date
from pathlib import Path

from harvester.parse import classify, date_from_filename, find_date, parse_direct_pdf, parse_page

FIX = Path(__file__).parent / "fixtures"
FILLER = "<p>" + "Allgemeine Informationen der Gemeinde. " * 5 + "</p>"


def page(body: str) -> str:
    return f"<html><body><nav><a href='/bplan'>Bebauungspläne</a></nav><main>{FILLER}{body}</main></body></html>"


def test_abensberg_real_page_keeps_only_bplan_notice():
    r = parse_page((FIX / "abensberg_bekanntmachungen.html").read_bytes(),
                   "https://www.abensberg.de/buergerservice/bekanntmachungen")
    assert len(r.notices) == 1  # election results, TenneT survey, FFH monitoring are filtered out
    n = r.notices[0]
    assert n.title == "Aufstellung eines einfachen Bebauungsplans „Altstadt\""
    assert (n.signal_type, n.procedure_step) == ("bplan", "fruehzeitige_beteiligung")
    assert (n.published_at, n.date_source) == (date(2026, 10, 26), "page")
    assert len(n.document_urls) == 7
    assert n.notice_url == "https://www.abensberg.de/bekanntmachungen/bauamt/Altstadt/SC-BAU-092_27102026_114207_051225.pdf"


def test_list_items_become_separate_notices_under_section_heading():
    html = page("""<h2>Laufende Verfahren</h2><ul>
      <li><a href="/a.pdf">Bebauungsplan Nr. 12 „Am Bach“ – öffentliche Auslegung</a> vom 03.09.2026</li>
      <li><a href="/b.pdf">8. Änderung des Flächennutzungsplans</a> Bekanntmachung vom 1. Oktober 2026</li>
      <li><a href="/c.pdf">Hundesteuersatzung 2026</a> vom 01.01.2026</li>
    </ul>""")
    r = parse_page(html, "https://example.bayern/bauleitplanung")
    got = [(n.signal_type, n.procedure_step, n.published_at, n.notice_url) for n in r.notices]
    assert got == [
        ("bplan", "auslegung", date(2026, 9, 3), "https://example.bayern/a.pdf"),
        ("fnp", "aenderung", date(2026, 10, 1), "https://example.bayern/b.pdf"),
    ]
    assert r.filtered == 1  # Hundesteuersatzung


def test_table_rows():
    html = page("""<table><tr><th>Plan</th><th>Datum</th></tr>
      <tr><td>Einbeziehungssatzung „Mühlweg“ – Satzungsbeschluss</td><td>12.08.2026</td><td><a href="/m.pdf">PDF</a></td></tr>
      <tr><td>Bebauungsplan „Gewerbe Süd“ – Inkrafttreten</td><td>02.07.2026</td><td><a href="/g.pdf">PDF</a></td></tr>
    </table>""")
    r = parse_page(html, "https://example.bayern/x")
    assert [(n.signal_type, n.procedure_step, n.published_at) for n in r.notices] == [
        ("other_planning", "satzungsbeschluss", date(2026, 8, 12)),
        ("bplan", "inkrafttreten", date(2026, 7, 2)),
    ]


def test_menu_labels_and_nav_are_not_notices():
    html = page('<p><a href="/bp">Bebauungspläne</a> | <a href="/fnp">Flächennutzungsplan</a></p>')
    r = parse_page(html, "https://example.bayern/")
    assert r.notices == [] and r.status == "no_planning_content"


def test_no_keyword_page_is_valid_empty_result():
    r = parse_page(page("<h2>Müllabfuhr</h2><p>Termine 2026</p>"), "https://example.bayern/")
    assert (r.notices, r.status) == ([], "no_planning_content")


def test_content_wrappers_with_noise_like_classes_survive():
    """Real cases: 'cc--no_sidebar', 'main-content sidebar-left' and an ASP.NET <form> wrapping the whole page."""
    notice = '<h3>Bebauungsplan „Nord“ – öffentliche Auslegung</h3><p>vom 01.09.2026</p><a href="/n.pdf">Plan</a>'
    for wrap in ('<div class="cc main cc--no_sidebar">{}</div>', '<div class="main-content sidebar-left">{}</div>',
                 '<form id="aspnetForm">{}</form>'):
        html = f"<html><body><div class='menu'><a href='/bp'>Bebauungsplan Süd</a></div>{wrap.format(FILLER + notice)}</body></html>"
        assert [n.title for n in parse_page(html, "https://e.bayern/").notices] == ["Bebauungsplan „Nord“ – öffentliche Auslegung"], wrap


def test_js_only_page_is_flagged():
    r = parse_page("<html><body><div id='app'></div><script>load()</script></body></html>", "https://e.bayern/")
    assert r.status == "empty_or_js_page"


def test_malformed_html_does_not_crash():
    html = page("<div><h3>Bebauungsplan „Ost“<p>Veröffentlicht am 31.02.2026 <a href='x.pdf'>Plan</a></div></span>")
    r = parse_page(html, "https://e.bayern/a/")
    assert len(r.notices) == 1
    assert r.notices[0].published_at is None  # 31.02. is invalid -> not guessed
    assert r.notices[0].date_source == "none"


def test_date_falls_back_to_pdf_filename_and_is_marked():
    html = page('<h3>Bebauungsplan „Altstadt“</h3><a href="/SC-BAU-092_27102026_114207.pdf">Bekanntmachung</a>')
    n = parse_page(html, "https://e.bayern/").notices[0]
    assert (n.published_at, n.date_source) == (date(2026, 10, 27), "filename")
    assert "date_from_filename" in n.flags


def test_doc_label_title_replaced_by_heading():
    html = page('<h3>Vorhabenbezogener Bebauungsplan „Solarpark“</h3><ul><li><a href="/s.pdf">Download</a></li></ul>')
    n = parse_page(html, "https://e.bayern/").notices[0]
    assert n.title.startswith("Vorhabenbezogener Bebauungsplan")


def test_pagination_same_host_only():
    html = page('<a rel="next" href="?page=2">weiter</a>')
    assert parse_page(html, "https://e.bayern/b").next_url == "https://e.bayern/b?page=2"
    html = page('<a href="https://other.de/2">weiter</a>')
    assert parse_page(html, "https://e.bayern/b").next_url is None
    html = page('<a href="/b/page/3/">weiter</a>')
    assert parse_page(html, "https://e.bayern/b/page/2/").next_url == "https://e.bayern/b/page/3/"
    html = page('<a href="/de/index.php?id=407&publish%5Bstart%5D=2">weiter</a>')  # real CMS pattern, new path
    assert parse_page(html, "https://e.bayern/de/rathaus-aktuell").next_url.endswith("publish%5Bstart%5D=2")
    for off_listing in ("/veranstaltungen/", "/b-plan-nord/"):  # real cases: events calendar, next blog post
        assert parse_page(page(f'<a href="{off_listing}">weiter</a>'), "https://e.bayern/b").next_url is None


def test_find_date_prefers_publication_label():
    assert find_date("Bebauungsplan vom 12.05.1998, geändert. Veröffentlicht am 26.10.2026") == date(2026, 10, 26)
    assert find_date("keine Angabe") is None


def test_classify_title_exclusion_beats_body_mention():
    assert classify("Kartier- und Vermessungsarbeiten TenneT", "im Bereich des Bebauungsplans Nr. 3") == (None, None)
    assert classify("Sanierungssatzung Stadellohe", "") == (None, None)
    assert classify("Aufstellungsbeschluss Bebauungsplan „Nord“", "")[1] == "aufstellungsbeschluss"
    assert classify("Verlängerung der Veränderungssperre für den Bebauungsplan Nr. 5", "")[1] is None
    # not land-use notices, even though they name a plan (real titles from the statewide run)
    for title in ("Datenschutzrechtliche Informationspflichten im Bauleitplanverfahren nach Art. 13 und 14 DSGVO",
                  "Satzung über ein besonderes Vorkaufsrecht im Bereich des Bebauungsplans Nr. 3",
                  "Planen & Bauen Digitaler Bauantrag Stadtentwicklung, ISEK Bauleitplanung Denkmalschutz",
                  "Bauen & Wohnen Grundstücke & Immobilien, Bauleitverfahren, Bebauungspläne"):
        assert classify(title, "")[0] is None, title
    # type only from the body: kept when the title names a step or a planning document, dropped otherwise
    body = "Bebauungsplan Nr. 12 „Am Bach“"
    assert classify("Erneute öffentliche Auslegung vom 16.10.2026", body) == ("bplan", "auslegung")
    assert classify("Vorentwurf Deckblatt Nr. 9", body)[0] == "bplan"
    assert classify("Nr. 30 Schwabering Ost – Plan", body)[0] == "bplan"
    # a long title listing the notice's documents, one of them the privacy sheet, is still the notice
    assert classify("Entwurf des Vorhabenbezogenen Bebauungs- und Grünordnungsplanes SO „Solarpark Meising“ "
                    "Bekanntmachung Plan Begründung mit Umweltbericht Datenschutzrechtliche Informationspflicht", "")[0] == "bplan"
    long_body = body + " " + "Allgemeine Informationen der Gemeinde. " * 20  # a page section, not one notice
    for title in ("Harald Schorner", "Stadtplanung", "Sonstige Bekanntmachungen"):
        assert classify(title, body)[0] is None and classify(title, long_body)[0] is None, title
    assert classify("Außenbereichsatzung „Frühlingsstraße“", "")[0] == "other_planning"
    assert classify("5. Flächennutzungsplanänderung", "")[1] == "aenderung"
    assert classify("Entwurf des Bebauungs- und Grünordnungsplanes SO „Solarpark“ sowie Änderung des FNP", "")[0] == "bplan"


def test_plural_section_labels_are_not_notices():
    html = page('<ul><li><a href="/x">Ortsabrundungs- und Außenbereichssatzungen</a></li><li><a href="/y">Bauleitplanungen</a></li></ul>')
    assert parse_page(html, "https://e.bayern/").notices == []


def test_filename_dates():
    assert date_from_filename("https://x/BP_2026-03-05_plan.pdf") == date(2026, 3, 5)
    assert date_from_filename("https://x/plan_99999999.pdf") is None


def test_direct_pdf():
    n = parse_direct_pdf("https://x.bayern/docs/Bebauungsplan_Nord_14082026.pdf")
    assert (n.signal_type, n.published_at, n.document_urls) == ("bplan", date(2026, 8, 14), [n.notice_url])
    assert "direct_pdf" in n.flags


def test_in_page_anchor_ids_do_not_change_record_url():
    """Real case (Eisenberg, Pforzen): accordion links get a random '#collapse-<id>' on every page load."""
    urls = set()
    for rnd in ("a124ee86", "77f0c1d2"):
        html = page(f'<div><a href="#collapse-{rnd}">Bebauungsplan Nr. 9 Ortskern Zell</a></div>'
                    f'<div><a href="/plan-nr-1/#top">Bebauungsplan Nr. 1 Zell</a></div>')
        urls |= {n.notice_url for n in parse_page(html, "https://e.bayern/bauleitplanung/").notices}
    assert urls == {"https://e.bayern/bauleitplanung/", "https://e.bayern/plan-nr-1/"}


def test_download_counters_and_sizes_do_not_change_the_title():
    """Real cases: Gnotzheim '… 396.1 KB 2.247 Download' (counter grows), Ingolstadt '( PDF, 69 kB )'."""
    titles = []
    for label in ("Bebauungsplan Nord 08.12.2022 396.1 KB 2.246 Download", "Bebauungsplan Nord 08.12.2022 396.1 KB 2.247 Downloads",
                  "Bebauungsplan Nord 08.12.2022 ( PDF, 69 kB )"):
        titles += [n.title for n in parse_page(page(f'<div><a href="/n.pdf">{label}</a></div>'), "https://e.bayern/").notices]
    assert titles == ["Bebauungsplan Nord 08.12.2022"] * 3


def test_notice_titled_only_with_a_date_gets_its_title_from_the_text():
    """Real case (Kinsau): a long list item whose bold part is only the date; plan and step are in the text."""
    text = ("Kinsau Bebauungsplan „Am Gänsstall-Nord Erweiterung“ Veröffentlichung der Planentwürfe gemäß § 13a Abs. 2 "
            "Nr. 1, § 13 Abs. 2 Nr. 2 und § 3 Abs. 2 BauGB vom 06.08.2026 bis einschließlich 07.09.2026")
    html = page(f'<ul><li><strong>06.08.2026</strong> {text} <a href="/e.pdf">Entwurf</a></li>'
                f'<li><strong>Harald Schorner</strong> Bauamt, Zimmer 3.3, Zuständigkeiten: Bauleitplanung, Bauanträge, '
                f'Bauberatung, Belange des Denkmalschutzes, Friedhofsverwaltung {"Telefon und Sprechzeiten. " * 5}</li></ul>')
    notices = parse_page(html, "https://e.bayern/").notices
    assert len(notices) == 1  # the contact block is dropped
    n = notices[0]
    assert n.title.startswith("06.08.2026 Kinsau Bebauungsplan „Am Gänsstall-Nord Erweiterung“")
    assert (n.signal_type, n.procedure_step, "title_from_text" in n.flags) == ("bplan", "auslegung", True)


def test_title_from_text_is_cleaned_too():
    """Review finding: a title taken from the notice text kept its download counter, so its id changed daily."""
    titles = []
    for count in ("2.247", "2.248"):
        html = page(f'<div><h3>06.08.2026</h3><a href="/a.pdf">Bekanntmachung Auslegung Bebauungsplan</a> '
                    f'(PDF, 120 KB) {count} Downloads</div>')
        titles += [n.title for n in parse_page(html, "https://e.bayern/").notices]
    assert len(titles) == 2 and titles[0] == titles[1] and "Download" not in titles[0]
