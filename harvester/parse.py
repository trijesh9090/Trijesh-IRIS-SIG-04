"""Step 3: read a town's web page and pull out the planning notices.

There are about 1,800 different sites, so instead of one parser per site there is one generic parser that
looks for planning keywords, works out which part of the page belongs to each notice, and reads the title,
date and PDF links from that part.
"""
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

# elements a notice title can sit in, list-like elements, and things we always throw away
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
ANCHOR_TAGS = (*HEADINGS, "a", "li", "td", "p", "strong", "summary", "dt")
LIST_TAGS = ("li", "td", "dt", "summary")
CODE_TAGS = ("script", "style", "noscript", "template", "iframe")
CHROME_TAGS = ("nav", "aside", "form", "header", "footer")
# class/id names that usually mean menus or page chrome ("nav", "main-nav", "menue_list", "cookie-banner").
# The lookbehinds avoid false hits such as "cc--no_sidebar", which on one real site wraps the actual content.
NOISE_ATTR = re.compile(r"(?<![a-z])(?<!no[-_])(nav|navbar|navigation|menu|menue|breadcrumbs?|footer|sidebar|cookie\w*)(?![a-z])")


def norm(text: str) -> str:
    """Prepare text for keyword matching: lowercase, umlauts written out (ä -> ae, ß -> ss),
    whitespace squashed. All keyword rules run on this.

    Args:
        text: any text, None is fine.

    Returns:
        The normalised text.
    """
    text = unicodedata.normalize("NFC", text or "").lower()
    text = text.translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "\xa0": " "}))
    return re.sub(r"\s+", " ", text).strip()


def clean(text: str) -> str:
    """Only squash whitespace. Used for text we store or show, so the original spelling is kept.

    Args:
        text: any text, None is fine.

    Returns:
        The cleaned text.
    """
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


# --- keywords (always matched against norm() text) ----------------------------------------------
# what makes something a B-Plan, a Flächennutzungsplan, or one of the other land-use statutes
BPLAN_RE = re.compile(r"bebauungsplae?n|bebauungs- ?und gruenordnungsplae?n|\bb-plae?n|\bbplan|vorhaben- und erschliessungsplan")
FNP_RE = re.compile(r"flaechennutzungsplae?n|\bfnp\b")
OTHER_RE = re.compile(
    r"bauleitplan|(einbeziehungs|ortsabrundungs|abrundungs|klarstellungs|aussenbereichs?|ergaenzungs)satzung"
    r"|satzung.{0,80}(einbeziehung|abrundung|klarstellung|aussenbereich)")
# anything a notice title might contain, plain "satzung" included; classify() makes the real decision later
ANCHOR_RE = re.compile(f"{BPLAN_RE.pattern}|{FNP_RE.pattern}|{OTHER_RE.pattern}|satzung")
# titles about these topics are not land-use notices, even if the text mentions a plan
EXCLUDE_RE = re.compile(
    r"\bwahl|wahlergebnis|haushalt|abfall|muell|strassensperr|\bjagd|hundesteuer|sanierungs"
    r"|kartier|vermessungsarbeiten|stellenausschreibung")
# Always dropped, even when the title names a plan: Vorkaufsrecht statutes (§ 25 BauGB, not a land-use plan) and
# site menus that slipped into the content ("Planen & Bauen ..."). Privacy sheets as well, unless they are just
# one item in a long title that lists all the documents of a notice.
NOT_A_NOTICE_RE = re.compile(r"vorkaufsrecht|^(planen|bauen|leben) (&|und) (bauen|wohnen)\b|toggle submenu|foerderprogramm")
PRIVACY_RE = re.compile(r"datenschutz|dsgvo")
# If the title has no plan word (so the type came from the text around it), the title still has to name a step or
# a planning document. Or the notice text has to be short and name a step itself: some real notices are titled
# only "06.08.2026" or "1. Bekanntmachung".
PLAN_DOC_RE = re.compile(
    r"entwurf|deckblatt|begruendung|geltungsbereich|baugebiet|sondergebiet|planzeichnung|satzung|\bplan\b|\bnr\.? ?\d")
NOTICE_SIZE = 600  # characters; anything longer is a whole page section, not a single notice


# procedure steps, most specific first; classify() tries the title before the text
STEPS = [
    ("inkrafttreten", r"inkrafttreten|in kraft getreten|tritt .{0,60}in kraft|rechtsverbindlich|§ ?10 abs\.? ?3"),
    ("satzungsbeschluss", r"satzungsbeschluss|als satzung beschlossen"),
    ("fruehzeitige_beteiligung", r"§ ?3 abs(atz|\.)? ?1"),
    ("auslegung", r"§ ?3 abs(atz|\.)? ?2|oeffentliche[n]? auslegung"),
    ("fruehzeitige_beteiligung", r"fruehzeitige[n]? (oeffentlichkeits)?(beteiligung|unterrichtung)"),
    ("auslegung", r"auslegung|veroeffentlichung im internet|veroeffentlichung der (plan)?entwuerfe"),
    ("aufstellungsbeschluss", r"aufstellungsbeschluss|aufstellung beschlossen|beschluss.{0,40}aufstellung"),
    ("aenderung", r"(?<!ver)aenderung"),  # but not inside "Veränderungssperre"
]


def has_plan_word(normed: str) -> bool:
    """Check if a text names a plan: a B-Plan, a Flächennutzungsplan or another land-use statute.

    Args:
        normed: text that already went through norm().

    Returns:
        True if it does.
    """
    return bool(BPLAN_RE.search(normed) or FNP_RE.search(normed) or OTHER_RE.search(normed))


def _has_step(normed: str) -> bool:
    """Check if a text names a procedure step (Auslegung, Satzungsbeschluss, ...).

    Args:
        normed: text that already went through norm().

    Returns:
        True if it does.
    """
    return any(re.search(pattern, normed) for _, pattern in STEPS)


# Words that make up menu labels and section headings ("Bebauungspläne", "Laufende Verfahren").
# A title built only from these is a label, not a notice.
GENERIC_WORDS = set(
    "die der des den und im in von zur zum fuer unsere aktuelle aktuell laufende laufend rechtskraeftige "
    "verfahren bauleitplanverfahren bebauungsplanverfahren uebersicht stadt gemeinde markt planung bauleitplanung bauleitplanverfahren bauleitplaene "
    "bebauungsplaene bebauungsplan flaechennutzungsplan flaechennutzungsplaene satzung satzungen "
    "bekanntmachungen bekanntmachung oeffentliche amtliche downloads informationen abgeschlossene online "
    "staedtebauliche aenderungen aenderungsverfahren ggf derzeit gueltige verbindliche ortsrecht alle aktuellen "
    "auf einen blick landesportal hier zum zur zu weitere auswahl geoportal bauleitverfahren".split())


def is_generic(normed: str) -> bool:
    """Check if a title is only a menu label or a section heading.

    Plurals count as labels too ("Bebauungspläne", "Ortsabrundungssatzungen"), because a single notice
    is about one plan, while a plural is the heading of a list.

    Args:
        normed: title that already went through norm().

    Returns:
        True if every word is a label word.
    """
    label_endings = ("-", "plaene", "satzungen", "verfahren", "planungen")
    for word in re.findall(r"[a-z0-9-]+", normed):
        if word not in GENERIC_WORDS and not word.endswith(label_endings):
            return False
    return True


# link texts that say nothing about the plan ("Download", "PDF", "hier")
DOC_LABEL_RE = re.compile(r"^(download|pdf|hier|mehr|weiter(lesen)?|bekanntmachung|link|datei|dokument)\b.{0,30}$")


def classify(title: str, body: str) -> tuple[str | None, str | None]:
    """Decide if a notice is about land-use planning, and which type and step it is.

    The title is checked first, then the text. Off-topic titles (elections, budget, ...), privacy
    sheets, Vorkaufsrecht statutes and leaked menus are dropped even if the text mentions a plan.

    Args:
        title: the notice title.
        body: the text of the notice.

    Returns:
        (signal_type, procedure_step), e.g. ("bplan", "auslegung"). signal_type is "bplan", "fnp"
        or "other_planning"; procedure_step can be None. (None, None) means: not a planning notice.
    """
    # normalised copies, only used for matching
    title_n, body_n = norm(title), norm(body)

    # the type comes from the title if it names a plan, otherwise from the text
    signal_type = None
    for text in (title_n, body_n):
        if BPLAN_RE.search(text):
            signal_type = "bplan"
        elif FNP_RE.search(text):
            signal_type = "fnp"
        elif OTHER_RE.search(text):
            signal_type = "other_planning"
        if signal_type:
            break
    if signal_type is None:
        return None, None

    # never a notice: Vorkaufsrecht statutes, leaked menus, and privacy sheets (unless the privacy sheet is just
    # one item in a long title that lists all the documents)
    if NOT_A_NOTICE_RE.search(title_n):
        return None, None
    if PRIVACY_RE.search(title_n) and (len(title_n) <= 120 or PRIVACY_RE.match(title_n)):
        return None, None

    # no plan word in the title: the title must name a step or a document, or the short notice text a step
    if not has_plan_word(title_n):
        title_is_useful = PLAN_DOC_RE.search(title_n) or _has_step(title_n)
        short_notice_with_step = len(body_n) <= NOTICE_SIZE and _has_step(body_n)
        if not title_is_useful and not short_notice_with_step:
            return None, None  # e.g. a contact person, "Rathaus" or "Bauen & Wohnen" that happens to sit next to a plan

    # an off-topic title wins over a plan mentioned in the text (e.g. a TenneT survey notice that names a B-Plan)
    if EXCLUDE_RE.search(title_n) and not (BPLAN_RE.search(title_n) or FNP_RE.search(title_n)):
        return None, None
    if signal_type == "other_planning" and EXCLUDE_RE.search(body_n):
        return None, None

    for text in (title_n, body_n):
        for step, pattern in STEPS:
            if re.search(pattern, text):
                return signal_type, step
    return signal_type, None


# --- dates: only dates that are actually written on the page or in a PDF name, never guessed ----------
MONTHS = "januar februar maerz april mai juni juli august september oktober november dezember".split()
# 26.10.2026, 26. 10. 2026 or 26. Oktober 2026
DATE_PAT = r"(\d{1,2})\.\s?(?:(\d{1,2})\.|\s?(" + "|".join(MONTHS) + r"))\s?(\d{4})"
# words that usually come right before the publication date
STRONG_LABEL = r"(?:veroeffentlicht(?: am)?|bekanntgemacht(?: am)?|bekanntmachung vom|datum|stand)\s*:?\s*"


def _mkdate(day, month, year) -> date | None:
    """Make a date, but only a sensible one.

    Args:
        day: day of the month.
        month: month number.
        year: the year. All three can be strings or numbers.

    Returns:
        The date, or None if it doesn't exist (31.02.) or the year is outside 1990 .. next year.
    """
    try:
        found = date(int(year), int(month), int(day))
    except ValueError:
        return None
    if 1990 <= found.year <= date.today().year + 1:
        return found
    return None


def _match_date(match) -> date | None:
    """Turn a date found by DATE_PAT into a real date.

    Args:
        match: the regex match (numeric month or month name).

    Returns:
        The date, or None if it isn't a real date.
    """
    day, month_number, month_name, year = match.groups()
    if month_number:
        month = month_number
    else:
        month = MONTHS.index(month_name) + 1
    return _mkdate(day, month, year)


def find_date(text: str) -> date | None:
    """Find the publication date in a notice text. Never guesses.

    Order: first a date after a publication label ("Veröffentlicht am", "bekanntgemacht", "Datum",
    "Stand"), then one after "vom", then simply the first date in the text.

    Args:
        text: the notice text.

    Returns:
        The date, or None if there is no valid date in the text.
    """
    text_n = norm(text)
    for pattern in (STRONG_LABEL + DATE_PAT, r"\bvom\s+" + DATE_PAT, DATE_PAT):
        for match in re.finditer(pattern, text_n):
            found = _match_date(match)
            if found:
                return found
    return None


def date_from_filename(url: str) -> date | None:
    """Find a date hidden in a PDF file name, e.g. SC-BAU-092_27102026_114207.pdf gives 27.10.2026.

    Used only when the page itself shows no date; such records get the "date_from_filename" flag.

    Args:
        url: the PDF link.

    Returns:
        The date, or None.
    """
    name = _file_name(url)
    patterns = ((r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)", "dmy"),
                (r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)", "ymd"),
                (r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", "ymd"),
                (r"(?<!\d)(\d{2})\.(\d{2})\.(20\d{2})(?!\d)", "dmy"))
    for pattern, order in patterns:
        for match in re.finditer(pattern, name):
            first, second, third = match.groups()
            if order == "dmy":
                found = _mkdate(first, second, third)
            else:
                found = _mkdate(third, second, first)
            if found:
                return found
    return None


# --- page -> notices ------------------------------------------------------------------------------
@dataclass
class Notice:
    """One planning notice as the parser found it, before it becomes a database row.

    Attributes:
        title: cleaned title.
        signal_type: "bplan", "fnp" or "other_planning".
        procedure_step: e.g. "auslegung", or None if not found.
        published_at: publication date, or None.
        date_source: "page", "filename" or "none".
        notice_url: first PDF, else the detail page, else the page itself.
        document_urls: all PDF links of the notice.
        flags: notes like "title_from_heading" or "date_from_filename".
    """
    title: str
    signal_type: str
    procedure_step: str | None
    published_at: date | None
    date_source: str  # page / filename / none
    notice_url: str
    document_urls: list[str]
    flags: list[str] = field(default_factory=list)


@dataclass
class PageResult:
    """Everything parse_page() found on one page.

    Attributes:
        notices: the planning notices.
        next_url: link to the next page of the same list, or None.
        status: "ok", "no_planning_content" or "empty_or_js_page".
        filtered: how many keyword hits were thrown away (off-topic, labels, loose sentences).
    """
    notices: list[Notice]
    next_url: str | None
    status: str  # ok / no_planning_content / empty_or_js_page
    filtered: int = 0  # keyword hits we threw away (off-topic, menu labels, loose sentences)


def _file_name(url: str) -> str:
    """Get the file name from a link, e.g. ".../Bebauungsplan%20Nord.pdf" gives "Bebauungsplan Nord.pdf".

    Args:
        url: any link.

    Returns:
        The last part of the path, with %20 and similar turned back into normal characters.
    """
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def is_pdf(url: str) -> bool:
    """Check if a link points to a PDF file.

    Args:
        url: any link.

    Returns:
        True if the path ends in .pdf.
    """
    return urlsplit(url).path.lower().endswith(".pdf")


def _strip_noise(soup: BeautifulSoup) -> Tag:
    """Remove scripts, menus, headers, footers and cookie banners so only the content is left.

    This is a guess based on tag and class names, with extra checks so it doesn't throw away a
    wrapper that actually holds the content.

    Args:
        soup: the parsed page. It is changed in place.

    Returns:
        The element to search for notices in: <main>, #content or <body>.
    """
    for element in soup.find_all(CODE_TAGS):
        element.decompose()

    # prefer <main> or #content, but only if it really has text: some sites put id="content" on an empty link
    root = None
    for candidate in (soup.find("main"), soup.find(id="content")):
        if candidate is not None and len(clean(candidate.get_text(" "))) >= 200:
            root = candidate
            break
    if root is None:
        root = soup.body or soup
    root_length = len(clean(root.get_text(" ")))

    for element in root.find_all(True):
        if element.decomposed or element.attrs is None or element.name in ("main", "article"):
            continue
        class_and_id = " ".join(element.get("class") or []) + " " + (element.get("id") or "")
        is_chrome = element.name in CHROME_TAGS
        if element.name in ("header", "footer") and element.find_parent(("main", "article")):
            is_chrome = False  # a header or footer inside the article belongs to the content
        if not is_chrome and not NOISE_ATTR.search(class_and_id.lower()):
            continue
        # This is a guess based on tag and class names. The next two checks stop it from removing a wrapper that
        # actually holds the content (an ASP.NET <form>, a "main-content sidebar-left" div) and the notices with it.
        if element.find(("h1", "main", "article")):
            continue
        if len(clean(element.get_text(" "))) >= 0.5 * root_length:
            continue
        element.decompose()
    return root


def _listing(url: str) -> tuple[str, str]:
    """Tell which list a page belongs to, ignoring the page number.

    Args:
        url: a page URL.

    Returns:
        (site, path without the page number), so '/amtstafel/page/3/' and '/amtstafel?page=2' both
        give '/amtstafel'.
    """
    parts = urlsplit(url)
    path = re.sub(r"(/(page|seite))?/\d+/?$", "", parts.path).rstrip("/")
    return parts.netloc, path


# page numbers in the query string: ?page=2, ?start=10, tx_news_pi1[currentPage]=2, publish[start]=2
PAGE_PARAM = re.compile(r"(?i)(page|start|seite|offset)(%5D|\])?=\d+")


def _next_page(soup: BeautifulSoup, page_url: str) -> str | None:
    """Find the link to the next page of the same list.

    A "weiter" that leads somewhere else (into an events calendar, to the next blog post) is ignored,
    otherwise we would keep clicking forever.

    Args:
        soup: the parsed page.
        page_url: the URL of this page.

    Returns:
        The next page's URL, or None.
    """
    for link in soup.find_all("a", href=True):
        rel = link.get("rel") or []
        text = norm(link.get_text(" "))
        css_class = " ".join(link.get("class") or []).lower()
        looks_like_next = "next" in rel or "next" in css_class or text in ("weiter", "naechste", "naechste seite", "»", "›", ">")
        if not looks_like_next:
            continue
        url = urljoin(page_url, link["href"]).split("#")[0]
        same_site = urlsplit(url).netloc == urlsplit(page_url).netloc
        same_list = _listing(url) == _listing(page_url) or PAGE_PARAM.search(urlsplit(url).query)
        if same_site and same_list and url != page_url.split("#")[0]:
            return url
    return None


def _is_anchor(element: Tag) -> bool:
    """Check if an element could be a notice title: it needs a plan word, can't be very long and
    can't be just a label like a "Bebauungsplan" download button.

    Args:
        element: a page element (heading, link, list item, ...).

    Returns:
        True if it is a candidate.
    """
    text = norm(element.get_text(" "))
    if not text or len(text) > 300 or not ANCHOR_RE.search(text):
        return False
    return not is_generic(text)  # e.g. a PDF button that just says "Bebauungsplan"


def _inside(element: Tag, box: list[Tag]) -> bool:
    """Check if an element belongs to a notice.

    Args:
        element: a page element.
        box: the elements that make up one notice.

    Returns:
        True if the element is one of them or sits somewhere inside one.
    """
    for part in box:
        if element is part:
            return True
        if any(parent is part for parent in element.parents):
            return True
    return False


def _heading_box(heading: Tag, root: Tag) -> list[Tag]:
    """Find the part of the page that belongs to a heading.

    That is its biggest parent that holds no other heading of the same level. If the heading sits
    alone in its wrapper, it is the heading plus the elements after it, up to the next heading.

    Args:
        heading: the heading.
        root: the content area; we never grow past it.

    Returns:
        The elements that make up the notice.
    """
    box = heading
    for _ in range(6):
        parent = box.parent
        if parent is None or parent is root or len(parent.find_all(heading.name)) > 1:
            break
        box = parent
    if box is not heading:
        return [box]

    # heading sits alone in its wrapper: take the following elements up to the next heading
    section = [heading]
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag):
            if sibling.name in HEADINGS or sibling.find(HEADINGS):
                break
            section.append(sibling)
    return section


def _generic_box(element: Tag, root: Tag, anchors: list[Tag]) -> list[Tag]:
    """Find the notice around a non-heading element (link, list item, paragraph, ...).

    Its list item or table row if it has one, otherwise grow upwards (at most 6 levels) until the
    next step would swallow another notice.

    Args:
        element: the element with the plan keyword.
        root: the content area.
        anchors: all candidate elements on the page.

    Returns:
        The elements that make up the notice.
    """
    if element.name in ("li", "tr"):
        row = element
    else:
        row = element.find_parent(("li", "tr"))
    if row is not None and _inside(row, [root]):
        return [row]

    box = element
    for _ in range(6):
        parent = box.parent
        if parent is None or parent is root or len(parent.find_all(HEADINGS)) > 1:
            break
        # stop before the box takes in the keyword of another notice
        takes_in_other_notice = any(other is not element and _inside(other, [parent]) and not _inside(other, [box])
                                    for other in anchors)
        if takes_in_other_notice:
            break
        box = parent
    return [box]


# file sizes and download counters in a link text change while the notice stays the same
# ("396.1 KB 2.247 Download" today, "... 2.248 Download" tomorrow), so they must not end up in the title
VOLATILE_RE = re.compile(r"\s+[\d.,]+\s?(downloads?|aufrufe|zugriffe|mal heruntergeladen|[kmg]b)\b", re.I)


def _abs(page_url: str, href: str) -> str:
    """Make a link absolute and drop the #part at the end.

    Args:
        page_url: the page the link is on.
        href: the link as written in the HTML, can be relative.

    Returns:
        The full URL.
    """
    return urljoin(page_url, href.strip()).split("#")[0]


def _extract(box: list[Tag], anchor: Tag, page_url: str) -> tuple[str, str, list[str], str | None, list[str]]:
    """Read title, text and links from one notice.

    If the title says nothing ("Download", "PDF"), the nearest heading or the PDF file name is used
    instead and flagged. File sizes and download counters are removed from the title.

    Args:
        box: the elements that make up the notice.
        anchor: the element where the plan keyword was found.
        page_url: the page URL, for making links absolute.

    Returns:
        (title, text, pdf_urls, detail_url or None, flags).
    """
    text = clean(" ".join(part.get_text(" ") for part in box))

    # all links in the notice (a box element can itself be a link)
    links = []
    for part in box:
        if part.name == "a" and part.get("href"):
            links.append(part)
        links += part.find_all("a", href=True)

    # PDF links, each once, in page order. The #part is dropped: some sites give accordions a random
    # '#collapse-<id>' on every page load, which would change the record id on every run.
    pdfs = []
    for link in links:
        url = _abs(page_url, link["href"])
        if is_pdf(url) and url not in pdfs:
            pdfs.append(url)

    detail = None
    if anchor.name == "a" and anchor.get("href"):
        href = _abs(page_url, anchor["href"])
        # a link to another page is a detail page; a jump within this page is not
        if not is_pdf(href) and href.startswith("http") and href != _abs(page_url, ""):
            detail = href

    flags = []
    title = clean(anchor.get_text(" "))
    if anchor.name in ("li", "tr", "td", "p") and len(title) > 200:  # long item: use its bold or linked part
        inner = anchor.find((*HEADINGS, "strong", "a"))
        if inner:
            title = clean(inner.get_text(" "))
        else:
            title = title[:200]

    if DOC_LABEL_RE.match(norm(title)):  # "Download" says nothing: use the nearest heading or the file name
        heading = None
        for part in box:
            found = [part] if part.name in HEADINGS else part.find_all(HEADINGS)
            if found:
                heading = found[0]
                break
        if not heading:
            heading = anchor.find_previous(HEADINGS)
        if heading is not None and clean(heading.get_text(" ")):
            title = clean(heading.get_text(" "))
            flags = ["title_from_heading"]
        elif pdfs:
            title = _file_name(pdfs[0])
            flags = ["title_from_filename"]

    return _clean_title(title), text, pdfs, detail, flags


def _clean_title(title: str) -> str:
    """Remove things from a title that change while the notice stays the same.

    Drops "(PDF, 1,2 MB)", "(pdf)", file sizes and download counters ("2.247 Downloads"), plus dashes and
    colons left at the ends. The title is part of the record id, so it must be the same on every run.

    Args:
        title: a title as found on the page.

    Returns:
        The cleaned title.
    """
    title = re.sub(r"\s*\(\s*(pdf|docx?)?[,\s]*[\d.,]+\s?[kmg]b\s*\)|\s*\(\s*(pdf|docx?)\s*\)", "", title,
                   flags=re.I).strip(" -–:")
    return VOLATILE_RE.sub("", title).strip(" -–:")


def parse_page(html: bytes | str, page_url: str) -> PageResult:
    """Turn one HTML page into planning notices.

    Args:
        html: the page as bytes or text.
        page_url: the page's URL, for links and for spotting the next page.

    Returns:
        A PageResult with the notices, the next page link and a status.
    """
    soup = BeautifulSoup(html, "html.parser")
    next_url = _next_page(soup, page_url)
    root = _strip_noise(soup)
    if len(clean(root.get_text(" "))) < 100:
        return PageResult([], next_url, "empty_or_js_page")

    # every element that could be a notice title; a hit inside another hit belongs to the outer one
    candidates = [element for element in root.find_all(ANCHOR_TAGS) if _is_anchor(element)]
    candidate_ids = {id(element) for element in candidates}
    anchors = []
    for element in candidates:
        if not any(id(parent) in candidate_ids for parent in element.parents):
            anchors.append(element)

    # Headings first. A heading above two or more list items (or above sub-headings) is a section title, and the
    # items are the notices. Paragraphs and links under a heading are just the body of that one notice.
    heading_boxes = []
    for heading in anchors:
        if heading.name not in HEADINGS:
            continue
        box = _heading_box(heading, root)
        inner = [other for other in anchors if other is not heading and _inside(other, box)]
        list_items = sum(other.name in LIST_TAGS for other in inner)
        has_sub_headings = any(other.name in HEADINGS for other in inner)
        if list_items < 2 and not has_sub_headings:
            heading_boxes.append((heading, box))

    # then everything that isn't already part of a heading's notice
    boxes = list(heading_boxes)
    for anchor in anchors:
        if anchor.name in HEADINGS:
            continue
        if any(_inside(anchor, box) for _, box in heading_boxes):
            continue
        boxes.append((anchor, _generic_box(anchor, root, anchors)))

    notices = []
    seen_boxes = set()
    filtered = 0
    for anchor, box in boxes:
        box_key = tuple(id(part) for part in box)
        if box_key in seen_boxes:  # two hits in the same box are the same notice
            continue
        seen_boxes.add(box_key)

        title, text, pdfs, detail, flags = _extract(box, anchor, page_url)
        signal_type, step = classify(title, text)

        # the title says nothing (just a date or "1. Bekanntmachung"): use the start of the notice text instead
        if signal_type and not has_plan_word(norm(title)) and len(text) <= NOTICE_SIZE:
            if len(text) > 200:
                title = text[:200].rsplit(" ", 1)[0]  # cut at a word boundary
            else:
                title = text
            title = _clean_title(title)  # same cleanup as any other title, so the record id stays stable
            flags.append("title_from_text")

        # date from the page first, then from a PDF name
        published = find_date(text)
        date_source = "page"
        if published is None:
            for pdf in pdfs:
                published = date_from_filename(pdf)
                if published:
                    date_source = "filename"
                    flags.append("date_from_filename")
                    break
        if published is None:
            date_source = "none"

        if signal_type is None or not title:
            filtered += 1
            continue
        # no date and no document: a menu label, or a loose sentence without a link
        if published is None and not pdfs:
            if is_generic(norm(title)) or (anchor.name == "p" and not detail):
                filtered += 1
                continue

        if pdfs:
            notice_url = pdfs[0]
        elif detail:
            notice_url = detail
        else:
            notice_url = page_url
        notices.append(Notice(title[:500], signal_type, step, published, date_source, notice_url, pdfs, flags))

    status = "ok" if notices else "no_planning_content"
    return PageResult(notices, next_url, status, filtered)


def parse_direct_pdf(url: str) -> Notice:
    """Make a notice for a town whose portal link goes straight to a PDF.

    All we know is the file name, so the record is flagged "direct_pdf" and ends up low confidence.

    Args:
        url: the PDF link.

    Returns:
        A Notice built from the file name.
    """
    name = _file_name(url)
    signal_type, step = classify(name, "")
    published = date_from_filename(url)
    flags = ["direct_pdf", "title_from_filename"]
    if not signal_type:
        flags.append("type_unknown")
        signal_type = "other_planning"
    if published:
        flags.append("date_from_filename")
        date_source = "filename"
    else:
        date_source = "none"
    return Notice(name, signal_type, step, published, date_source, url, [url], flags)
