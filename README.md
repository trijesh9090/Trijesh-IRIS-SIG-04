# IRIS-SIG-04 — Bauleitplanung Harvester (Bavaria)

## What it does

Bavarian towns publish public notices when they make or change a land-use plan: a **Bebauungsplan**
(B-Plan, the detailed plan for one area) or a **Flächennutzungsplan** (FNP, the plan for the whole town).

This harvester collects those notices for all 2,056 Bavarian municipalities and stores them in PostgreSQL /
PostGIS (table `iris_staging.signal_ingest`). Each notice is stored with its title, publication date, links,
town and a map point in EPSG:25832. You can run it again at any time: new notices are added, changed ones are
updated, and nothing is ever stored twice.

## How it works

```
 Bauleitplanungsportal (state portal)
 gemeinden.json: 2,056 towns, each with its planning page and a map point
          │
          ▼
 1. Load municipalities   read the list, check each town's key and point
          │
          ▼
 2. Select towns          apply --region / --district / --ags,
          │               pick each town's best link
          ▼
 3. Extract notices       visit each town's website politely,
          │               look for German planning words
          │               (Bebauungsplan, Flächennutzungsplan, …),
          │               read title, date and PDF links,
          │               drop everything off-topic
          ▼
 4. Remove duplicates     the same notice listed twice counts once
          │
          ▼
 5. Store in database     insert new, update changed, never a duplicate
          │
          ▼
 6. Build report          report.json + dashboard (report.html)
```

The websites are in German and all built differently, so one generic parser looks for German planning words
instead of using a separate parser per site. Details: [docs/DETAILS.md](docs/DETAILS.md).

## How to run

You need **Docker** and **[uv](https://docs.astral.sh/uv/)**. uv installs the right Python by itself.

**1. Set up (once)**

```bash
cp .env.example .env     # your local settings (see below)
docker compose up -d     # starts the database and creates the table
uv sync                  # installs the Python packages
```

The database is PostgreSQL 16 with PostGIS 3.4 and listens on `localhost:5433`.

In `.env`, replace `contact@example.com` in `HARVESTER_USER_AGENT` with your own e-mail, so site owners
can reach you. Keep the name in front of the `/`: robots.txt rules are matched against it. The default
password `change-me` is fine for a local test. If you change it, change it in **both** places
(`POSTGRES_PASSWORD` and inside `DATABASE_URL`) before the first `docker compose up`.

**2. Run it** (this visits the real town websites, so you need internet)

```bash
# quick try: 24 towns, about 15 s, without the database
uv run --env-file .env python -m harvester --district Kelheim --dry-run

# one region: 258 towns, about 2 min
uv run --env-file .env python -m harvester --region Niederbayern

# all of Bavaria: 2,056 towns, about 11 min
uv run --env-file .env python -m harvester
```

Add **`--live`** to any of these to watch the run step by step in your browser. The page switches to the
dashboard when the run is done; press Ctrl+C in the terminal to close it.

**3. Look at the results**

**Dashboard:** open `data/runs/<run_id>/report.html` in a browser. It shows the new and changed notices,
the latest notices, a map, and which towns had problems.

**Database:** the 20 newest notices:

```bash
docker compose exec db psql -U iris -d iris -c "
  SELECT published_at, municipality, title
  FROM iris_staging.signal_ingest
  WHERE published_at <= current_date
  ORDER BY published_at DESC LIMIT 20"
```

**Checks:** duplicates, bad points and missing links must all be 0:

```bash
docker compose exec -T db psql -U iris -d iris -f - < sql/validation.sql
```

**4. Run the tests**

```bash
uv run --env-file .env pytest
```

70 tests, no internet needed. The 4 database tests are skipped if the database isn't running.

**Useful options:** `--region`, `--district`, `--ags` (one or more town keys), `--limit N`, `--dry-run` (no
database), `--live`, and `--portal-file tests/fixtures/gemeinden.json` (use the saved town list instead of the
live portal). All options: `uv run python -m harvester --help`. Settings (`DATABASE_URL`, `HARVESTER_USER_AGENT`)
come from `.env`; there are no passwords in the code.

## Data source

| Step | Source |
|---|---|
| Town list | `https://geoportal.bayern.de/bauleitplanungsportal/data/gemeinden.json`: one JSON file the portal page loads; it has no API. 2,056 towns with key (AGS), name, district, region, map point (already EPSG:25832) and up to three links. |
| Notices | each town's own planning page. We use `url_2` (ongoing procedures), else `url_1` (general page); never `url_3` (finished plans). |

Access is polite and lawful: public pages only, an honest User-Agent with a contact address, robots.txt
respected (RFC 9309), one request at a time and a 2 s pause per site, nothing blocked is bypassed.
*Datenquellen: Bayerische Vermessungsverwaltung – www.geodaten.bayern.de und bayerische Gemeinden.*

## What gets stored

One row per notice in `iris_staging.signal_ingest` ([sql/schema.sql](sql/schema.sql)):

| Field | Meaning |
|---|---|
| `source_record_id` | town key + a fingerprint of the notice link and title; the same notice always gets the same id |
| `signal_type`, `procedure_step` | `bplan` / `fnp` / `other_planning`, and the step (e.g. `auslegung`), from German keywords |
| `title`, `published_at`, `date_source` | as written on the page; the date is never guessed (`NULL` + flag if missing) |
| `notice_url`, `source_page_url`, `document_urls` | the notice's PDF or page, the page we read, all PDF links |
| `ags`, `municipality`, `district`, `region`, `geom` | from the town list; `geom` is the town's midpoint as an EPSG:25832 point |
| `confidence`, `quality_flags` | how complete the record is, and what is missing (e.g. `missing_date`) |
| `first_seen_at`, `last_seen_at`, `updated_at` | when first found, last seen, and last changed |

Full mapping rules: [docs/DETAILS.md](docs/DETAILS.md#mapping-to-iris_stagingsignal_ingest).

## Sample output and report

- [sample/](sample/): the staging table as CSV, run reports and dashboards, logs and the output of the
  validation queries, from real runs over all of Bavaria.
- [REPORT.md](REPORT.md): error and quality report (how much was collected, re-run results, errors per
  reason, a manual precision check).

## Known limitations

- **The portal is being retired on 31.10.2026** and replaced by DiPlan. The saved town list
  (`--portal-file tests/fixtures/gemeinden.json`) keeps the harvester working; DiPlan is the next source to add.
- **One parser for ~1,800 different websites:** some notices are missed and about 8% of records are page
  fragments rather than notices; these are mostly marked `low` confidence.
- **About 65% of records have no date,** because many pages only list plan documents. The date is left empty
  and flagged, never guessed.
- **Points are town midpoints,** not the exact plan area.
- **About 10% of sites can't be read:** 6% refuse automated access (HTTP 403) and 4% forbid it in robots.txt
  or don't serve robots.txt. They are reported, not worked around.

Full list and error handling: [docs/DETAILS.md](docs/DETAILS.md).
