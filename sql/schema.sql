-- Staging table for the planning notices (PostgreSQL 16 / PostGIS 3.4).
-- Everything uses IF NOT EXISTS, so the harvester runs this file before every load without harm.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS iris_staging;

CREATE TABLE IF NOT EXISTS iris_staging.signal_ingest (
    id               bigserial PRIMARY KEY,
    source           text        NOT NULL,
    source_record_id text        NOT NULL,
    signal_type      text        NOT NULL CHECK (signal_type IN ('bplan', 'fnp', 'other_planning')),
    procedure_step   text        CHECK (procedure_step IN ('aufstellungsbeschluss', 'fruehzeitige_beteiligung',
                                     'auslegung', 'satzungsbeschluss', 'inkrafttreten', 'aenderung')),
    title            text        NOT NULL CHECK (btrim(title) <> ''),
    published_at     date,
    date_source      text        NOT NULL CHECK (date_source IN ('page', 'filename', 'none')),
    notice_url       text        NOT NULL,
    source_page_url  text        NOT NULL,
    link_type        text        NOT NULL CHECK (link_type IN ('ongoing', 'general')),
    document_urls    jsonb       NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(document_urls) = 'array'),
    ags              char(8)     NOT NULL CHECK (ags ~ '^09[0-9]{6}$'),
    municipality     text        NOT NULL,
    district         text,
    region           text,
    geom             geometry(Point, 25832),
    geo_status       text        NOT NULL CHECK (geo_status IN ('municipality_centroid', 'unresolved')),
    parser           text        NOT NULL,
    confidence       text        NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    quality_flags    jsonb       NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(quality_flags) = 'array'),
    content_hash     text        NOT NULL,
    run_id           text        NOT NULL,
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    -- the same notice can only exist once; a re-run updates the row instead of adding a new one
    CONSTRAINT signal_ingest_source_uk UNIQUE (source, source_record_id),
    -- Safety net if the Python checks ever miss something: no point outside Bavaria (EPSG:25832, metres),
    -- no "centroid" without a point, and no date without saying where it came from.
    CONSTRAINT signal_ingest_geom_bavaria CHECK (
        geom IS NULL OR (ST_X(geom) BETWEEN 480000 AND 900000 AND ST_Y(geom) BETWEEN 5230000 AND 5620000)),
    CONSTRAINT signal_ingest_geo_status CHECK ((geom IS NULL) = (geo_status = 'unresolved')),
    CONSTRAINT signal_ingest_date_source CHECK ((published_at IS NULL) = (date_source = 'none'))
);

-- for map queries, lookups by town and "what's new" by date
CREATE INDEX IF NOT EXISTS signal_ingest_geom_gix ON iris_staging.signal_ingest USING gist (geom);
CREATE INDEX IF NOT EXISTS signal_ingest_ags_ix ON iris_staging.signal_ingest (ags);
CREATE INDEX IF NOT EXISTS signal_ingest_published_ix ON iris_staging.signal_ingest (published_at);
