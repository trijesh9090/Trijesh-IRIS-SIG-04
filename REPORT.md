# Error & Quality Report — IRIS-SIG-04

**Scope:** all of Bavaria, 2,056 municipalities in 7 Regierungsbezirke. Live runs on 27.10.2026 with
8 parallel workers, one request at a time per host and a 2 s delay per host. Each run took about 11 minutes.
**Source verified:** `gemeinden.json`, HTTP 200, `Last-Modified: Tue, 27 Oct 2026 22:15:05 GMT` (run A). The
file is regenerated about every 10 minutes and holds 2,056 municipalities.
**Evidence:** [sample/](sample/):
- `signal_ingest_bayern.csv`: the staging table;
- `report_run{A,B}.json` and `report_run{A,B}.html`: step timeline, error breakdown, map and per-town table;
- `run{A,B}.log`: run logs;
- `validation_output.txt`: output of the validation queries.

## 1. How much was used: links, pages and filters

| Step | Count |
|---|---|
| Municipalities in the portal | 2,056, all processed; no `--limit` and no region filter |
| Links chosen | **1,815**: 1,183 `url_2` (ongoing procedures) + 632 `url_1` (general page, used only when there is no `url_2`) |
| Links crawled | **1,802**. 13 were skipped as a council information system (11) or a map viewer (2). |
| Municipalities without a usable link | 241: 230 `no_link`, 11 `only_url_3`. `url_3` (completed plans) is never used, as decided in the source research. |
| Pages fetched | 1,786 HTML pages, plus one `robots.txt` per host |
| Municipalities with more than one page | 20; the deepest had 38 pages (Speichersdorf). **No municipality hit the page limit** (`pagination_capped` = 0). |
| Planning-keyword hits dropped as not relevant | 5,685 (see section 4) |
| Duplicates removed within a run | 346, the same notice listed twice on one site |
| Records loaded | **11,443** from 1,054 municipalities |

**Caps and filters still in place:**
- `--max-pages 100` is a safety guard. It is reported when hit, and `0` removes it. It used to be 3, which
  silently cut off 2 sites, so it was raised.
- The 8 MB response cap applies per page.
- Titles are truncated at 500 characters.

There is no cap on the number of municipalities, links or records.

## 2. Load and re-run

| Run | Records | Inserted | Unchanged | Updated in place | Rejected by DB |
|---|---|---|---|---|---|
| A `20261027T221546Z` (empty table) | 11,443 | 11,443 | 0 | 0 | 0 |
| B `20261027T222630Z` (straight re-run) | 11,417 | **0** | 11,417 | 0 | 0 |

- **No duplicates after the re-run.** Validation query 2 returns 0 rows. Every record B found was already
  in A, with the same `id` and `first_seen_at`.
- **26 rows were not seen in B.** Their 2 sites failed during run B (Nersingen with a network error,
  Lalling with HTTP 403). The rows were kept with run A's `last_seen_at`, which validation query 8 shows.
  Rows are never deleted on a temporary failure.
- **Updates in place, checked live:** an earlier run, `sample/report_runC.json` (`20261027T205434Z`), ran after
  a fix to the procedure-step parser. It updated 48 rows in place, moving `updated_at`, and inserted 0.

Comparing record IDs across runs found four inputs that change by themselves. Each one would have created a
"new" record for the same notice on every run:

| Cause | Example | Fix |
|---|---|---|
| Random accordion anchors | `#collapse-a124ee86…` on 3 sites | URL fragments are dropped; an in-page anchor is not a detail link |
| Signed CDN links | `?Expires=…&Signature=…` (Erbendorf) | ignored for the record ID and hash; the stored URL stays the fresh one |
| Download-plugin tokens | `&refresh=6ae118b3…` (Niederaichbach) | ignored in the same way |
| Download counters in titles | "396.1 KB 2.247 Download" (Gnotzheim) | sizes and counters are removed from titles |

## 3. Municipality outcomes (run A)

| Status | Count | Reason |
|---|---|---|
| ok | 1,054 | at least 1 planning notice extracted |
| no_planning_content | 532 | the page was read but had no planning notice (valid empty result) |
| error | 182 | `http_403` 103: the server refuses a non-browser User-Agent. Not bypassed. |
| | | `robots_disallowed` 42, `robots_unreachable` 31: robots.txt is respected (RFC 9309) |
| | | `http_500` 3, `http_404` 2, `http_401` 1 |
| skipped | 288 | `no_link` 230, `only_url_3` 11, `empty_or_js_page` 34, `council_system` 11, `map_viewer` 2 |

## 4. Relevance: is every record a land-use planning item?

The task asks for municipal land-use updates and B-Plan notices. Every candidate goes through
`classify()` in [harvester/parse.py](harvester/parse.py), and the rules are tested in `tests/test_parse.py`:

1. **A planning keyword is required:** Bebauungsplan / B-Plan / vorhabenbezogener / Bebauungs- und
   Grünordnungsplan → `bplan`; Flächennutzungsplan / FNP → `fnp`; Bauleitplan, Einbeziehungs-,
   Ortsabrundungs-, Klarstellungs-, Außenbereichs- or Ergänzungssatzung → `other_planning`. A page with no
   keyword gives no records.
2. **Off-topic titles are dropped** even if the body mentions a plan: elections, budget, waste, road
   closures, hunting, dog tax, Sanierungsgebiet, survey works, job offers.
3. **Always dropped, even when the title names a plan:**
   - GDPR information sheets ("Datenschutzrechtliche Informationspflichten …"), unless the sheet is one item
     in a long title listing the notice's documents;
   - Vorkaufsrecht statutes (§ 25 BauGB, not a land-use plan);
   - leaked navigation menus ("Planen & Bauen …", "Bauen & Wohnen …").
4. **Menu labels and section headings are dropped** when they have no date and no document, for example
   "Bebauungspläne", "Laufende Verfahren" or "Bebauungspläne (Auswahl)". Explanatory prose without a date,
   document or link is dropped too.
5. **If the type comes only from the body text,** the title must name a step or a planning document, or the
   notice text must be short and name a step. In that case the title is taken from the text
   (`title_from_text`, 85 rows); for example a heading "06.08.2026" becomes "06.08.2026 Kinsau Bebauungsplan
   „Am Gänsstall-Nord Erweiterung" Veröffentlichung der Planentwürfe …". A contact person, "Rathaus" or an
   e-mail address next to a plan mention is dropped.

These rules dropped 5,685 keyword hits in run A. Compared with the previous run, rules 3 and 5 removed
about 225 privacy sheets, 15 leaked menus, the Vorkaufsrecht statutes and about 25 noise items such as a
contact person, "Rathaus" or "Homepage".

**Manual check of the final table:** 60 random rows (`setseed(0.61)`):
- **0 / 60 are about an off-topic subject** (elections, budget, privacy, Vorkaufsrecht and so on).
- **55 / 60 (≈ 92%) are real land-use planning items:** B-Plan, FNP or Ortsabrundungs- /
  Einbeziehungssatzung notices and documents. About 5 of these have vague titles, for example
  "Bebauungs- und Grünordnungsplan" or "Gesamtbebauungsplan Teil II". The plan name is in the PDF.
- **5 / 60 are planning-related page fragments, not notices:**
  - an explanatory sentence ("Bebauungs- und Flächennutzungspläne werden in einem gesetzlich geregelten
    Verfahren aufgestellt …");
  - a legal boilerplate fragment ("dass nicht fristgerecht abgegebene Stellungnahmen …");
  - a menu label ("Ortsplanung/ Bebauungspläne");
  - a generic label ("Bebauungsplan in Aufstellung");
  - an e-mail address.

  These are mostly flagged `low` confidence, and filtering on `confidence <> 'low'` or
  `published_at IS NOT NULL` removes most of them.
- **About 5 rows of known residual noise remain.** Two menu variants ("Bauen & Projekte …", "Bauen &
  Umwelt …"), one privacy sentence and one section heading that lists three towns are still in the table.

## 5. Record quality (11,443 rows, 1,054 municipalities)

| Measure | Value |
|---|---|
| signal_type | bplan 7,860 · fnp 2,274 · other_planning 1,309 |
| region | Oberbayern 3,496 · Schwaben 1,896 · Niederbayern 1,639 · Unterfranken 1,624 · Oberpfalz 1,022 · Oberfranken 977 · Mittelfranken 789 |
| confidence | high 2,981 (26%) · medium 5,130 (45%) · low 3,332 (29%) |
| `published_at` present | 4,000 (35%). Of those, 923 come only from a PDF filename (`date_source=filename`). |
| `published_at` missing | 7,443 (65%), flagged `missing_date`, never guessed |
| procedure_step found | 5,422 (47%); the rest are flagged `step_unknown` |
| no PDF documents | 3,689 (32%), flagged `no_documents` |
| from a url_1 fallback page | 6,324 (55%), flagged `fallback_url_1` |
| published in the last 3 / 12 months | 364 / 1,064 |
| future dates | 8, flagged `date_in_future`. These are mostly the end of a display period ("Auslegung bis 16.11.2026") picked up as the date. |

**Why so many dates are missing:** more than half of the rows come from `url_1` "general" planning pages.
These list current plan documents ("Bebauungsplan X – Begründung.pdf") rather than dated notices. To get
dated **updates** only, filter on `published_at IS NOT NULL`, `link_type = 'ongoing'` or
`confidence = 'high'`.

**Dates inside PDFs were tested and not adopted.** We probed 11 undated notices whose official
"Bekanntmachung" PDF could be fetched. 8 were scanned images with no text layer. In the 3 with text, the
first date found was a council meeting date, not the posting date. Reliable recovery would need OCR plus a
rule for picking the right date.

## 6. Geometry

| Check | Result |
|---|---|
| Rows with a geometry | 11,443 / 11,443 (`municipality_centroid`) |
| Wrong SRID / invalid / outside the Bavaria envelope | 0 / 0 / 0 |
| WGS84 cross-check (lon 8.9–13.9, lat 47.2–50.6) | 0 outside |
| Unresolved | 0 |

All 2,056 portal midpoints pass validation: each is inside the Bavaria envelope and inside its own bounding
box. Invalid coordinates are still guarded against in Python, where they give `geom NULL` + `unresolved` +
a flag, and in the database, where a `CHECK` rejects the row. Both paths are tested.

## 7. Open issues (not fixable inside this assignment)

1. **The portal is retired on 31.10.2026.** The next step is to switch level 1 to DiPlan
   (`digitale.planung.bayern.de`). Until then, `--portal-file tests/fixtures/gemeinden.json` works.
2. **Sites that refuse access:** HTTP 403 on 103 sites (5%) and robots.txt blocks or failures on 73.
   Blocks are not bypassed. The fix is an allow-list from these municipalities, or their notices via DiPlan.
3. **Records per PDF on document-list pages.** Grouping files into procedures would need per-site rules.
4. **65% of records have no date.** This mostly comes from static plan lists. OCR on PDFs would be the
   next lever.
5. **Residual page fragments (≈ 8% of rows in the sample check).** These are explanatory sentences, labels
   and contact lines on planning pages. They are mostly `low` confidence.
