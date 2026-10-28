-- Checks on the staging table after a run. The ones marked "must be 0" should return nothing.
-- Run: docker compose exec -T db psql -U iris -d iris -f - < sql/validation.sql

\echo '1. Volume by region / signal type'
SELECT region, signal_type, count(*) FROM iris_staging.signal_ingest GROUP BY 1, 2 ORDER BY 1, 2;

\echo '2. Duplicates on the business key (must be 0 rows; also enforced by UNIQUE constraint)'
SELECT source, source_record_id, count(*) FROM iris_staging.signal_ingest GROUP BY 1, 2 HAVING count(*) > 1;

\echo '3. Geometry: SRID, validity, inside Bavaria envelope (bad_* must be 0)'
SELECT count(*) FILTER (WHERE geom IS NOT NULL)                                AS with_geom,
       count(*) FILTER (WHERE geom IS NOT NULL AND ST_SRID(geom) <> 25832)     AS bad_srid,
       count(*) FILTER (WHERE geom IS NOT NULL AND NOT ST_IsValid(geom))       AS bad_invalid,
       count(*) FILTER (WHERE geom IS NOT NULL AND NOT ST_Within(geom,
             ST_MakeEnvelope(480000, 5230000, 900000, 5620000, 25832)))        AS bad_outside_bavaria,
       count(*) FILTER (WHERE geo_status = 'unresolved')                       AS unresolved
FROM iris_staging.signal_ingest;

\echo '4. Cross-check: point in WGS84 should be inside Bavaria lon 8.9-13.9 / lat 47.2-50.6 (must be 0 rows)'
SELECT ags, municipality, ST_AsText(ST_Transform(geom, 4326))
FROM iris_staging.signal_ingest
WHERE geom IS NOT NULL
  AND NOT ST_Within(ST_Transform(geom, 4326), ST_MakeEnvelope(8.9, 47.2, 13.9, 50.6, 4326));

\echo '5. Missing fields (visibility, not errors)'
SELECT count(*)                                              AS total,
       count(*) FILTER (WHERE published_at IS NULL)          AS missing_date,
       count(*) FILTER (WHERE date_source = 'filename')      AS date_from_filename,
       count(*) FILTER (WHERE procedure_step IS NULL)        AS missing_step,
       count(*) FILTER (WHERE document_urls = '[]'::jsonb)   AS no_documents,
       count(*) FILTER (WHERE published_at > current_date)   AS future_date
FROM iris_staging.signal_ingest;

\echo '6. Confidence and quality flags'
SELECT confidence, count(*) FROM iris_staging.signal_ingest GROUP BY 1 ORDER BY 1;
SELECT flag, count(*) FROM iris_staging.signal_ingest, jsonb_array_elements_text(quality_flags) flag
GROUP BY 1 ORDER BY 2 DESC;

\echo '7. Traceability: every row has source URLs and an ags-prefixed id (must be 0)'
SELECT count(*) FROM iris_staging.signal_ingest
WHERE notice_url !~ '^https?://' OR source_page_url !~ '^https?://' OR source_record_id NOT LIKE ags || ':%';

\echo '8. Re-run evidence: rows seen in more than one run keep first_seen_at < last_seen_at'
SELECT run_id AS last_run, count(*) AS rows, count(*) FILTER (WHERE first_seen_at < last_seen_at) AS seen_before
FROM iris_staging.signal_ingest GROUP BY 1 ORDER BY 1;

\echo '9. Latest notices'
SELECT published_at, municipality, signal_type, procedure_step, left(title, 70) AS title
FROM iris_staging.signal_ingest ORDER BY published_at DESC NULLS LAST LIMIT 15;
