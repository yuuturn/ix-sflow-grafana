-- Simulates Grafana's macro expansion for every dashboard panel so the SQL can
-- be validated without a browser. $__timeFilter(bucket) becomes a BETWEEN on a
-- Z-suffixed literal, and $__timeFrom()/$__timeTo() become Z-suffixed literals
-- cast to timestamp (PostgreSQL drops the zone for `timestamp`, which is what
-- we want because everything is stored in UTC).
\set from_ts '''2026-10-19T13:20:00Z'''
\set to_ts   '''2026-10-19T14:00:00Z'''
\set direction 'all'
\set topn 12

\timing on

\echo '--- panel 1: current total bps ---'
SELECT sum(bytes) * 8 / 300.0 AS "合計"
FROM flow_5m
WHERE bucket = (SELECT max(bucket) FROM flow_5m
                 WHERE bucket BETWEEN :from_ts AND :to_ts);

\echo '--- panel 2/3: current in/out bps ---'
SELECT direction, sum(bytes) * 8 / 300.0 AS bps
FROM flow_5m
WHERE direction IN ('inbound', 'outbound')
  AND bucket = (SELECT max(bucket) FROM flow_5m WHERE bucket BETWEEN :from_ts AND :to_ts)
GROUP BY 1;

\echo '--- panel 4: total volume in range ---'
SELECT sum(bytes) AS "合計"
FROM flow_5m
WHERE bucket BETWEEN :from_ts AND :to_ts
  AND ( :'direction' IN ('all','すべて') OR direction = :'direction');

\echo '--- panel 10: sankey source/target/value ---'
SELECT source, target, value
FROM sankey_flows(:from_ts::timestamp, :to_ts::timestamp, :'direction', :topn)
ORDER BY value DESC
LIMIT 8;

\echo '--- panel 11: throughput timeseries ---'
SELECT bucket AS time,
       sum(CASE WHEN direction = 'inbound'  THEN bytes ELSE 0 END) * 8 / 300.0 AS "ダウンロード",
       sum(CASE WHEN direction = 'outbound' THEN bytes ELSE 0 END) * 8 / 300.0 AS "アップロード"
FROM flow_5m
WHERE bucket BETWEEN :from_ts AND :to_ts
GROUP BY bucket ORDER BY bucket
LIMIT 3;

\echo '--- panel 12: per-AS timeseries ---'
WITH top AS (
    SELECT dst_asn, dst_org, sum(bytes) AS b
    FROM flow_5m
    WHERE bucket BETWEEN :from_ts AND :to_ts
      AND dst_host = '0.0.0.0'::inet
      AND ( :'direction' IN ('all','すべて') OR direction = :'direction')
    GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 8
)
SELECT g.bucket AS time, 'AS' || g.dst_asn || ' ' || g.dst_org AS metric,
       sum(g.bytes) * 8 / 300.0 AS value
FROM flow_5m g JOIN top t ON t.dst_asn = g.dst_asn
WHERE g.bucket BETWEEN :from_ts AND :to_ts AND g.dst_host = '0.0.0.0'::inet
GROUP BY g.bucket, metric ORDER BY g.bucket
LIMIT 3;

\echo '--- panel 13: destination ranking ---'
SELECT CASE WHEN dst_host <> '0.0.0.0'::inet THEN host(dst_host)
            WHEN dst_asn > 0 THEN 'AS' || dst_asn || ' ' || dst_org
            ELSE '(未解決)' END AS "宛先",
       sum(bytes) * 8 / 300.0 / 1000000 AS "平均 (Mbps)",
       sum(bytes) AS "転送量"
FROM flow_5m
WHERE bucket BETWEEN :from_ts AND :to_ts
  AND ( :'direction' IN ('all','すべて') OR direction = :'direction')
GROUP BY 1 ORDER BY sum(bytes) DESC LIMIT 5;

\echo '--- panel 14: source ranking ---'
SELECT CASE WHEN src_host <> '0.0.0.0'::inet THEN host(src_host)
            WHEN src_asn > 0 THEN 'AS' || src_asn || ' ' || src_org
            ELSE '(未解決)' END AS "送信元",
       sum(bytes) * 8 / 300.0 / 1000000 AS "平均 (Mbps)",
       sum(bytes) AS "転送量"
FROM flow_5m
WHERE bucket BETWEEN :from_ts AND :to_ts
  AND ( :'direction' IN ('all','すべて') OR direction = :'direction')
GROUP BY 1 ORDER BY sum(bytes) DESC LIMIT 5;

\echo '--- panel 15: service port ranking ---'
SELECT CASE svc_port WHEN 443 THEN '443 https' WHEN 80 THEN '80 http'
                     WHEN 53 THEN '53 dns' WHEN 123 THEN '123 ntp'
                     WHEN 993 THEN '993 imaps' WHEN 853 THEN '853 dot'
                     WHEN 0 THEN '(不明)' ELSE svc_port::text END AS "サービス",
       sum(bytes) * 8 / 300.0 / 1000000 AS "平均 (Mbps)",
       sum(bytes) AS "転送量"
FROM flow_5m
WHERE bucket BETWEEN :from_ts AND :to_ts
  AND ( :'direction' IN ('all','すべて') OR direction = :'direction')
GROUP BY 1 ORDER BY sum(bytes) DESC LIMIT 5;

\echo '--- verify the Z-suffixed literal really is UTC ---'
SELECT '2026-10-19T07:00:00Z'::timestamp AS parsed,
       now() AT TIME ZONE 'utc' AS utc_now,
       current_setting('TimeZone') AS session_tz;
