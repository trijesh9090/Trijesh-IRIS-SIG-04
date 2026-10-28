# IRIS-SIG-04 — technical details

The [README](../README.md) explains what the harvester does and how to run it. This page has the details
behind it: access rules, how the parser works, how records are identified, every error case, the test
list and the full list of limitations.

## Contents

1. [Polite access](#polite-access)
2. [How the parser finds notices](#how-the-parser-finds-notices)
3. [Mapping to `iris_staging.signal_ingest`](#mapping-to-iris_stagingsignal_ingest)
4. [Command-line options and run output](#command-line-options-and-run-output)
5. [When something goes wrong](#when-something-goes-wrong)
6. [Tests](#tests)
7. [Known limitations (full list)](#known-limitations-full-list)
8. [Project layout](#project-layout)

## Polite access

**Access:** public pages only, honest User-Agent with a contact address, `robots.txt` honoured per
RFC 9309: the longest matching rule wins, `*` and `$` wildcards work, and an unreachable robots.txt means full
disallow. Python's `urllib.robotparser` gets the first two wrong, so the harvester has its own small matcher.
One request at a time and 2 s delay per host (`--workers` only parallelises across different hosts).
Redirects are followed step by step (at most 5), and every step goes through robots.txt and the per-host delay
of its own host. Retries with backoff respect `Retry-After` on 429/5xx, but never wait more than 60 s.
There is an 8 MB response cap. No logins or blocks are bypassed.
Attribution as required by the portal terms: *Datenquellen: Bayerische Vermessungsverwaltung –
www.geodaten.bayern.de und bayerische Gemeinden*.

## How the parser finds notices

The **generic parser** ([harvester/parse.py](../harvester/parse.py)) handles any page; there are no per-site
parsers. It works in these steps:

1. Strip noise: `script/style/nav/header/footer/aside/form` and anything whose class or id contains
   `nav|menu|breadcrumb|footer|sidebar|cookie`. Then work inside `<main>`, else `#content`, else `<body>`.
2. Find anchor elements (`h1–h6, a, li, td, p, strong, summary, dt`) whose text contains a planning keyword,
   after lowercasing and folding umlauts (ä→ae, ß→ss). Bare labels such as a "Bebauungsplan" download
   button or a "Bebauungspläne" menu link are not anchors.
3. Grow each anchor into its **notice box**:
   - a heading grows up to the largest parent that holds no other heading of its level, or takes its
     following siblings when it sits alone in a wrapper;
   - list items and table rows use their `li` / `tr`;
   - anything else grows up to 6 levels until a second notice would be included.
   A heading over two or more list items counts as a section title, and the items become the notices.
   A paragraph that mentions a plan but has no date, document or link counts as prose, not a notice.
4. From each box take the title, date and PDF links, then classify it:
   - **Date:** a date after "Veröffentlicht am / bekanntgemacht / Datum / Stand" first, then one after
     "vom", then the first valid date. Numeric and month-name formats are both read. If the page has no
     date, a date in a PDF filename is used and marked `date_source=filename`. Otherwise the date is
     `NULL` and flagged. Dates are never guessed, and invalid ones such as 31.02. are dropped.
   - **Type:** `bplan` / `fnp` / `other_planning`, checked on the title first, then the box text.
   - **Exclusion:** notices whose title names a non-planning topic (elections, budget, waste, hunting,
     Sanierungsgebiet, survey works, job offers) are dropped, even if the body mentions a B-Plan. Always
     dropped, even when the title names a plan: GDPR information sheets attached to procedures ("Datenschutz-
     rechtliche Informationspflichten …"), Vorkaufsrecht statutes (§ 25 BauGB, not a land-use plan) and
     navigation menus that leaked into the content ("Planen & Bauen …", "Bauen & Wohnen …").
     Veränderungssperren are kept: they always name the B-Plan being drawn up.
   - **Type from the body only:** if the title has no plan keyword, the record is kept only if the title names a
     step or a planning document (Entwurf, Deckblatt, Begründung, Plan, Nr. …), or the notice text is short
     (≤ 600 characters) and names a step. In that case the title is taken from the notice text (flag
     `title_from_text`). A heading like "06.08.2026" becomes "06.08.2026 Kinsau Bebauungsplan „Am Gänsstall-Nord
     Erweiterung" Veröffentlichung der Planentwürfe …", while a contact block, "Rathaus" or "Stadtplanung" next to
     a plan mention is dropped.
   - **Procedure step:** `inkrafttreten > satzungsbeschluss > auslegung (§3(2)) > fruehzeitige_beteiligung
     (§3(1)) > aufstellungsbeschluss > aenderung`, checked on the title first, then the body.
5. Pagination: follow `rel=next`, "weiter" or "nächste" links that stay on the same listing (same host and
   path apart from a page number, e.g. `?page=2` or `/page/3/`), up to `--max-pages` (default 100, `0` = no
   limit; a page seen twice ends the loop). A "weiter" into an events calendar or to the next blog post is
   not followed. If more pages exist than allowed, the municipality is
   reported with the reason `pagination_capped`, so a cap never truncates data silently.

## Mapping to `iris_staging.signal_ingest`

The schema is in [sql/schema.sql](../sql/schema.sql) and is idempotent; it is applied on every run and at
container start.

| Column | Source / rule |
|---|---|
| `source` | constant `bauleitplanungsportal_by` |
| `source_record_id` | `ags:` + `sha256(notice_url + "|" + lower(title))[:16]`, stable across runs: URL fragments (`#collapse-<random>` accordions) are dropped, rotating URL parameters (CDN `Expires`/`Signature`/`X-Amz-*`, WordPress Download Manager `refresh`) are ignored for the id and hash, and file sizes / download counters ("2.247 Downloads") are removed from titles |
| `signal_type`, `procedure_step` | keyword rules above |
| `title` | anchor text; if that is only "Download/PDF/hier", the box heading, else the PDF filename (flagged) |
| `published_at`, `date_source` | `page` / `filename` / `none` (DB check: `NULL` ⇔ `none`) |
| `notice_url` | first PDF in the notice, else the detail link, else the scraped page |
| `source_page_url`, `link_type` | the scraped page; `ongoing` (url_2) / `general` (url_1, also flagged `fallback_url_1`) |
| `document_urls` | all absolute PDF links of the notice (jsonb array) |
| `ags`, `municipality`, `district`, `region` | portal `ags`, `name`, `kreis`, `bezirk` |
| `geom`, `geo_status` | `ST_SetSRID(ST_MakePoint(midpoint), 25832)` → `municipality_centroid`; invalid → `NULL` + `unresolved` + flag |
| `parser` | `generic` |
| `confidence` | `high` = date + link, `medium` = link (PDF or detail page), `low` = neither, or a direct-PDF link |
| `quality_flags` | `missing_date`, `date_from_filename`, `date_in_future`, `no_documents`, `step_unknown`, `fallback_url_1`, `title_from_heading`, `title_from_filename`, `title_from_text`, `direct_pdf`, geo problems |
| `content_hash` | `sha256(title, date, step, sorted documents)`; `updated_at` only moves when this or `signal_type` changes |
| `run_id`, `first_seen_at`, `last_seen_at`, `created_at`, `updated_at` | run bookkeeping; `first_seen_at` / `created_at` are never overwritten |

**Incremental retrieval:** municipal sites have no "changed since" API, feeds or ETags we could rely on,
so every run reads the current pages again (a few requests per municipality). What makes it incremental
is the load step:
- new notices are inserted, with `first_seen_at` set once;
- known ones only get `last_seen_at` and `run_id` updated;
- changed ones (a different `content_hash`) are updated in place, and `updated_at` moves;
- notices that are no longer published remain, with an old `last_seen_at`.

`WHERE first_seen_at >= <run start>` gives "new since the last run".

**Idempotency:** the table has `UNIQUE (source, source_record_id)`. Rows are written with
`INSERT … ON CONFLICT DO UPDATE`, and duplicates within a run are removed before loading. Each row gets
its own savepoint, so a row the database rejects is reported in `report.json` and does not abort the run.

**Coordinates:** notices almost never contain coordinates, so every record gets the municipality midpoint
(`geo_status = municipality_centroid`). The midpoint is accepted only if it lies inside the Bavaria
envelope in EPSG:25832 (x 480–900 km, y 5,230–5,620 km) and inside the municipality's bounding box.
WGS84 degrees, missing values or out-of-box points give `geom = NULL`, `geo_status = unresolved` and a
named flag. A `CHECK` constraint in the database rejects any point outside the envelope as a last line of
defence.

## Command-line options and run output

Options: `--region`, `--district`, `--ags …`, `--limit N`, `--portal-file PATH`, `--max-pages 100` (0 = no
limit), `--workers 8`, `--delay 2.0`, `--timeout 20`, `--out-dir data/runs`, `--dry-run`, `--live`, `--port 8765`. There are no credentials in the
source; `DATABASE_URL` and `HARVESTER_USER_AGENT` come from the environment.

Every run writes `data/runs/<run_id>/`:
- `raw/gemeinden.json` and `raw/<ags>_p<n>.html` (the raw evidence);
- `records.jsonl` (exactly what was loaded);
- `report.json`, with a status and reason per municipality, counts of flags, confidence and types, and load
  statistics (inserted / unchanged / updated / rejected).
- `report.html`, the run dashboard (self-contained; open it in a browser). It shows only:
  - **headline numbers:** towns with notices, records, new and changed this run, towns with errors or skipped;
    a one-line run summary expands to the step timings;
  - **new & changed in this run:** the actual new signals, searchable, with a link to each notice;
  - **latest notices** by publication date, plus records by type, region and procedure step;
  - **map** of every town, coloured by status and sized by number of notices;
  - **problems:** errors and skips by reason, with the towns behind each;
  - **all towns** (collapsed): search, status filter, links to the live and the saved raw page; click a town
    to see its records.

  The map needs internet access (Leaflet + OpenStreetMap tiles). Rebuild the dashboard for any run with
  `uv run python -m harvester.html_report data/runs/<run_id>`.

**Watching a run live:** add `--live`. A small server from the Python standard library starts on
`http://127.0.0.1:8765/` (localhost only; `--port` to change it) and opens in the browser. The page shows
the six steps one by one (load municipalities → select towns → extract notices → remove duplicates → store
in database → build dashboard). Each step is shown as waiting, running or done, with its time and numbers.
During extraction it shows a progress bar with the time left, live ok / empty / error / skipped counters,
the towns being worked on and the ones just finished. When the run ends, the page switches to the
dashboard. The server keeps serving it until you press Ctrl+C. Saved municipal pages are served as plain
text, so their scripts never run.

## When something goes wrong

| Problem | What the pipeline does | Where you see it |
|---|---|---|
| `HARVESTER_USER_AGENT` or `DATABASE_URL` missing | Stops before doing anything (exit code 2) | error line in the log |
| Portal unreachable or its file unusable (empty, not a list) | Stops after 3 retries; nothing in the database changes (exit code 1). Use `--portal-file` with the saved snapshot. | error line; failed step in the `--live` page |
| Malformed portal entry (bad AGS, no name) | That town is skipped (`malformed_entry`, `invalid_ags`, `missing_name`); the run goes on | report, dashboard "Problems" |
| Invalid coordinates (WGS84 degrees, outside Bavaria, outside the town's box) | Record kept without a point: `geom NULL`, `geo_status = unresolved` plus a named flag. The database also rejects such points via `CHECK`. | `quality_flags`, validation queries 3 and 4 |
| Site slow, down or overloaded (timeout, 429, 5xx) | Retried 3 times with backoff (honouring `Retry-After`), then that town is marked `error` with a reason; the run goes on | report, dashboard "Problems" |
| Site refuses access (403/401, robots.txt disallow, robots.txt unreachable) | Not bypassed; the town is marked `error` with the reason (`http_403`, `robots_disallowed`, or `robots_unreachable:<cause>` such as `robots_unreachable:timeout`) | same |
| Empty body / JavaScript-only page / no planning notices | `empty_response` / `empty_or_js_page` (skipped) / `no_planning_content` (a valid empty result) | same |
| Page 2+ of a listing fails (network error, not HTML, unparsable) | Earlier pages are kept and counted; reason `pagination_<code>` | same |
| More pages than `--max-pages` | Stops at the limit; reason `pagination_capped` | same |
| Broken HTML, impossible dates (31.02.) | Parsed anyway; the invalid date is dropped and flagged, never guessed; a parser crash becomes `parse_error` for that town | flags, report |
| Same notice twice | Removed in the run (`duplicates_in_run`); across runs the unique key makes it an update, not a new row | report, validation query 2 |
| A row violates a database rule | Only that row is rejected (savepoint per row) and listed; the others are stored | `load.rejected` in the report |
| Database down during the store step | Nothing is written (one transaction); the run still writes `records.jsonl`, `report.json` and the dashboard, which says so; exit code 1. The next run stores everything. | error line, dashboard note |
| An unexpected error while scraping one town (a bug, a URL that can't be parsed) | That town is marked `error` with `unexpected_error:<type>` and logged with its traceback; the other towns go on | report, log |
| Redirect loop or more than 5 redirects | `too_many_redirects` for that town | report |
| Ctrl+C during a run | Towns not started yet are cancelled, the ones in progress finish their current request, nothing is stored; exit code 130 | log |
| A town that worked before fails now | Its rows stay in the table with the older `last_seen_at`; nothing is deleted | validation query 8 |

## Tests

`pytest` runs 70 tests and needs no network:
- **Parser:** the real Abensberg page (exactly 1 notice; the election, TenneT and FFH notices are filtered
  out), list, table and menu pages, keyword-free and JS-only pages, malformed HTML, invalid dates, the
  filename date fallback, title fallback, pagination (same listing only; a "weiter" into a calendar or
  the next blog post is not followed), random in-page anchor ids, and content wrappers with noise-like class names
  (taken from real sites).
- **Portal:** link choice, AGS checks, rejection of WGS84, out-of-box and missing coordinates, a malformed
  entry and an empty portal file, plus the saved 2,056-municipality snapshot.
- **Dashboard and live view:** EPSG:25832 → WGS84 conversion checked against PostGIS, scraped titles cannot
  break out of the page's data block, new/changed records are marked, step and town progress is tracked,
  and the live server serves the page, status and run files (saved pages as plain text only).
- **HTTP fetcher,** against a local stdlib server: retries on 5xx, `Retry-After` on 429 (and its 60 s cap),
  reason codes for 404 / 500 / empty body / timeout / too large / SSL failure, robots.txt disallow (the page is
  never requested), robots.txt 404 meaning allow vs 5xx meaning block, the RFC 9309 rules (longest match,
  wildcards, own group), redirects checked step by step (also into a forbidden path, and redirect loops), and
  the per-host delay (also with parallel workers).
- **Pipeline:** stable record ids for rotating CDN-signed URLs, network failure, an unexpected error in one
  town, pagination failure (earlier pages are kept), the pagination cap being reported, non-HTML and empty
  responses, page decoding (one bad byte doesn't garble the page), invalid coordinates and in-run duplicates,
  using a fake fetcher.
- **Stopping cleanly:** an unreachable, missing or empty portal list stops the run with exit code 1 before
  anything is written; a missing `HARVESTER_USER_AGENT` or `DATABASE_URL` stops it with exit code 2; a database outage
  still writes the records, report and dashboard.
- **Database** (rolled back after each test): a re-run produces no duplicates, keeps `id`, `first_seen_at` and
  `updated_at` and moves `last_seen_at` (the row is first aged by a day, so the timestamps really differ);
  changed content or a reclassification updates the existing row and moves `updated_at`; and the DB rejects
  bad coordinates and reports it.

## Known limitations (full list)

- **The portal is being retired.** A notice on the portal says it will be replaced on **31.10.2026** by
  the DiPlan Portal (`https://www.digitale.planung.bayern.de/`). After that date the live
  `gemeinden.json` URL may disappear. `tests/fixtures/gemeinden.json` (snapshot from 27.10.2026) with
  `--portal-file` keeps the pipeline runnable. DiPlan, which is XPlanung-based, is the alternative to
  integrate next.
- **Generic HTML heuristics.** About 1,800 differently built sites are read by one parser. Expect some
  missed notices (JS-rendered pages, notices only inside PDFs, unusual layouts) and some noise: static
  plan lists on `url_1` pages appear as `medium` / `low` confidence records without dates. The
  confidence and flags are there to make this visible rather than hide it. Shared sites such as
  `landkreis-cham.de` (28 towns, with no dates on the page) are not given a special parser.
- **Coordinates are municipality centroids,** not plan locations. Exact geometry would need the plan PDFs
  or the XPlanung data from DiPlan.
- **Record identity depends on `notice_url` + title.** If a municipality re-uploads the same notice under a
  new PDF name, it becomes a new record. The old one stays, with an older `last_seen_at`. Rows are never
  deleted; "disappeared" notices can be found with `last_seen_at < <run start>`.
- **The portal's `Last-Modified` is not a content date.** `gemeinden.json` is regenerated about every
  10 minutes, so this header changes even when the content does not. It is logged as "source verified at",
  not used for incremental loading. Incremental behaviour comes from the upsert plus
  `first_seen_at` / `last_seen_at`.
- **Procedure-step detection is heuristic.** It uses the most specific keyword in the title, then the body.
  Notices that recap earlier steps can be misclassified.
- **Some sites block the crawler.** About 6% of sites answer `403` to an honest, non-browser User-Agent,
  and about 4% disallow crawling in `robots.txt` or don't serve robots.txt at all (which RFC 9309 treats as a
  full disallow). They are reported (`http_403`, `robots_disallowed`) and not
  worked around.
- **Pages that list documents give one record per PDF.** On `url_1` pages that list each plan file
  ("Bebauungsplan X – Begründung.pdf", "… – Planzeichnung.pdf"), each file becomes its own record. Grouping
  files into procedures would need per-site rules.
- **Untraceable mentions are dropped on purpose.** A paragraph that names a plan but has no date, document
  or link is treated as explanatory prose and dropped. Real but undocumented items, such as a plan named
  only in body text, are lost this way.
- **Not covered:** municipalities with no usable link, council systems and map viewers. These are listed
  with their reasons in `report.json`.
- **Dates inside PDFs are not read.** A probe of 11 undated notices whose "Bekanntmachung" PDF could be
  fetched found 8 scanned images with no text layer. In the 3 with text, the first date found was a council
  meeting date, not the posting date. Recovering them would need OCR plus a rule for picking the right date,
  so the date stays `NULL` and is flagged rather than guessed.
- **One link per municipality.** As decided in the source research, `url_2` (ongoing) is used and `url_1`
  only as a fallback; `url_3` (completed plans) is never used.
- **Notices without their own link are identified by the page they are on.** If a notice has no PDF and no
  detail link, its `notice_url` is the listing page. On a paginated list such a notice gets a new id if it
  moves to another page, and the old row stays with an older `last_seen_at`.
- **A town linking straight to a big PDF.** The file is downloaded to see what it is; over 8 MB it becomes
  `too_large` and gives no record (no such town in the current portal data).
- **"raw" links in a dashboard opened from disk.** Through `--live` the saved town pages are served as plain
  text. Opened directly from `data/runs/…`, the browser shows them as the original web page, with the
  town site's own scripts.

## Project layout

```
harvester/
  __main__.py      command line: options and settings check, then pipeline.run()
  pipeline.py      the six steps of a run, and harvest() for one town
  portal.py        step 1-2: municipality list, link choice, coordinate checks
  http.py          polite fetching: User-Agent, robots.txt, per-site delay, retries
  parse.py         step 3: one generic parser, page -> notices (keywords, dates, pagination)
  records.py       notice -> database row: stable id, content hash, flags, confidence, dedupe
  load.py          step 5: idempotent upsert into iris_staging.signal_ingest
  live.py          progress tracking and the --live page server
  html_report.py   step 6: the dashboard (report.html)
  templates/       live.html and dashboard.html
sql/               schema.sql (table + checks), validation.sql (checks after a run)
tests/             pytest suite + fixtures (saved portal list, real Abensberg page)
sample/            output of real runs, validation output
```
