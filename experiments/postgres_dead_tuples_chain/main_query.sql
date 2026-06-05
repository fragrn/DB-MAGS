SELECT
    a.region,
    a.segment,
    e.category_id,
    (e.account_id % 97) AS bucket,
    count(*) AS event_count,
    sum(e.amount) AS total_amount
FROM fact_events e
JOIN dim_accounts a
  ON a.account_id = e.account_id
WHERE e.tenant_id = 42
  AND e.score >= 900
GROUP BY
    a.region,
    a.segment,
    e.category_id,
    (e.account_id % 97)
ORDER BY
    total_amount DESC,
    event_count DESC,
    a.region,
    a.segment,
    e.category_id,
    bucket
LIMIT 500;
