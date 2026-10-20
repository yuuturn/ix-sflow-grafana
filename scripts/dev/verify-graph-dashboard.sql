-- 検証用: 積み上げ面グラフと AS 別サマリの SQL（マクロは展開済み）
\set from_ts '''2026-10-19T09:30:00Z'''
\set to_ts   '''2026-10-19T15:30:00Z'''
\set direction 'all'
\set topn 10

\echo '=== 積み上げ面: (time, metric, value) ==='
WITH base AS (
    SELECT bucket,
           CASE WHEN direction = 'inbound' THEN src_asn ELSE dst_asn END AS asn,
           CASE WHEN direction = 'inbound' THEN src_org ELSE dst_org END AS org,
           bytes
    FROM flow_5m
    WHERE bucket BETWEEN :from_ts AND :to_ts
      AND direction IN ('inbound', 'outbound')
      AND (:'direction' IN ('all', 'inbound', 'outbound') OR direction = :'direction')
),
labelled AS (
    SELECT bucket, 'AS' || asn || ' ' || org AS label, bytes FROM base WHERE asn > 0
    UNION ALL
    SELECT bucket, '(未解決)', bytes FROM base WHERE asn <= 0
),
tot AS (SELECT label, sum(bytes) AS t FROM labelled GROUP BY 1),
top AS (SELECT label FROM tot ORDER BY t DESC LIMIT :topn),
final AS (
    SELECT l.bucket, CASE WHEN tp.label IS NULL THEN 'その他' ELSE l.label END AS metric, l.bytes
    FROM labelled l LEFT JOIN top tp ON tp.label = l.label
)
SELECT bucket AS time, metric, sum(bytes) * 8 / 300.0 AS value
FROM final
GROUP BY 1, 2
ORDER BY 1
LIMIT 12;

\echo '=== AS 別サマリ ==='
WITH base AS (
    SELECT bucket,
           CASE WHEN direction = 'inbound' THEN src_asn ELSE dst_asn END AS asn,
           CASE WHEN direction = 'inbound' THEN src_org ELSE dst_org END AS org,
           bytes
    FROM flow_5m
    WHERE bucket BETWEEN :from_ts AND :to_ts
      AND direction IN ('inbound', 'outbound')
      AND (:'direction' IN ('all', 'inbound', 'outbound') OR direction = :'direction')
),
labelled AS (
    SELECT bucket, 'AS' || asn || ': ' || org AS label, bytes FROM base WHERE asn > 0
    UNION ALL
    SELECT bucket, '(未解決)', bytes FROM base WHERE asn <= 0
),
per_bucket AS (
    SELECT label, bucket, sum(bytes) * 8 / 300.0 AS bps, sum(bytes) AS bytes
    FROM labelled GROUP BY 1, 2
)
SELECT
    label                                                     AS "AS",
    min(bps)                                                  AS "MIN",
    max(bps)                                                  AS "MAX",
    (array_agg(bps ORDER BY bucket DESC))[1]                  AS "LAST",
    avg(bps)                                                  AS "AVERAGE",
    percentile_cont(0.95) WITHIN GROUP (ORDER BY bps)         AS "~95TH",
    sum(bytes)                                                AS "TOTAL"
FROM per_bucket
GROUP BY label
ORDER BY sum(bytes) DESC
LIMIT :topn;

\echo '=== 単位確認: 合計 bps は妥当か ==='
SELECT
    sum(bytes) AS total_bytes,
    round(sum(bytes) * 8 / 300.0 / 1e6, 2) AS mbps_per_bucket_sum,
    round(sum(bytes) * 8 / (6.5 * 3600) / 1e6, 3) AS avg_mbps_over_window
FROM flow_5m
WHERE bucket BETWEEN :from_ts AND :to_ts AND direction IN ('inbound', 'outbound');
