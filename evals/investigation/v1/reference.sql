-- name: overall_churn
SELECT CASE quarter_start
    WHEN '2026-01-01' THEN '2026-Q1' ELSE '2026-Q2' END AS quarter,
  SUM(CASE WHEN LOWER(CAST(active_at_start AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END) AS denominator,
  SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END) AS churned,
  ROUND(1.0 * SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END) /
    NULLIF(SUM(CASE WHEN LOWER(CAST(active_at_start AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END), 0), 4) AS churn_rate
FROM uploaded_data
WHERE quarter_start IN ('2026-01-01', '2026-04-01')
GROUP BY quarter_start ORDER BY quarter_start;

-- name: segment_plan_churn
SELECT CASE quarter_start
    WHEN '2026-01-01' THEN '2026-Q1' ELSE '2026-Q2' END AS quarter,
  customer_segment, plan_tier,
  COUNT(*) AS denominator,
  SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END) AS churned,
  ROUND(1.0 * SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END) / COUNT(*), 4) AS churn_rate
FROM uploaded_data
WHERE LOWER(CAST(active_at_start AS TEXT)) IN ('true', '1')
  AND quarter_start IN ('2026-01-01', '2026-04-01')
GROUP BY quarter_start, customer_segment, plan_tier
ORDER BY quarter_start, customer_segment, plan_tier;

-- name: region_churn
SELECT CASE quarter_start
    WHEN '2026-01-01' THEN '2026-Q1' ELSE '2026-Q2' END AS quarter,
  region, COUNT(*) AS denominator,
  SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END) AS churned,
  ROUND(1.0 * SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END) / COUNT(*), 4) AS churn_rate
FROM uploaded_data
WHERE LOWER(CAST(active_at_start AS TEXT)) IN ('true', '1')
  AND quarter_start IN ('2026-01-01', '2026-04-01')
GROUP BY quarter_start, region ORDER BY quarter_start, region;

-- name: smb_basic_tenure
SELECT CASE quarter_start
    WHEN '2026-01-01' THEN '2026-Q1' ELSE '2026-Q2' END AS quarter,
  CASE WHEN CAST(tenure_months_start AS INTEGER) < 6 THEN 'new' ELSE 'established' END AS tenure_group,
  COUNT(*) AS denominator,
  SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END) AS churned,
  ROUND(1.0 * SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END) / COUNT(*), 4) AS churn_rate
FROM uploaded_data
WHERE LOWER(CAST(active_at_start AS TEXT)) IN ('true', '1') AND customer_segment = 'SMB' AND plan_tier = 'Basic'
  AND quarter_start IN ('2026-01-01', '2026-04-01')
GROUP BY quarter_start, tenure_group ORDER BY quarter_start, tenure_group;

-- name: mrr_loss
SELECT CASE quarter_start
    WHEN '2026-01-01' THEN '2026-Q1' ELSE '2026-Q2' END AS quarter,
  ROUND(SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN CAST(mrr_at_start AS REAL) ELSE 0 END), 2) AS mrr_lost
FROM uploaded_data
WHERE quarter_start IN ('2026-01-01', '2026-04-01')
GROUP BY quarter_start ORDER BY quarter_start;

-- name: missing_reasons
SELECT
  SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END) AS churned,
  SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') AND COALESCE(churn_reason_code, '') = '' THEN 1 ELSE 0 END) AS missing,
  ROUND(1.0 * SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') AND COALESCE(churn_reason_code, '') = '' THEN 1 ELSE 0 END) /
    NULLIF(SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END), 0), 4) AS missing_rate
FROM uploaded_data;
