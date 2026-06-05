CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

DROP TABLE IF EXISTS fact_events CASCADE;
DROP TABLE IF EXISTS dim_accounts CASCADE;

CREATE TABLE dim_accounts (
    account_id integer PRIMARY KEY,
    region text NOT NULL,
    segment text NOT NULL,
    vip_tier integer NOT NULL
);

CREATE TABLE fact_events (
    event_id bigserial PRIMARY KEY,
    account_id integer NOT NULL REFERENCES dim_accounts(account_id),
    tenant_id integer NOT NULL,
    category_id integer NOT NULL,
    score integer NOT NULL,
    amount numeric(12, 2) NOT NULL,
    payload text NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
) WITH (
    autovacuum_enabled = off,
    toast.autovacuum_enabled = off
);

INSERT INTO dim_accounts (account_id, region, segment, vip_tier)
SELECT
    gs,
    CASE gs % 6
        WHEN 0 THEN 'north'
        WHEN 1 THEN 'south'
        WHEN 2 THEN 'east'
        WHEN 3 THEN 'west'
        WHEN 4 THEN 'central'
        ELSE 'international'
    END,
    CASE gs % 5
        WHEN 0 THEN 'enterprise'
        WHEN 1 THEN 'smb'
        WHEN 2 THEN 'consumer'
        WHEN 3 THEN 'public'
        ELSE 'partner'
    END,
    1 + (gs % 4)
FROM generate_series(1, 20000) AS gs;

INSERT INTO fact_events (
    account_id,
    tenant_id,
    category_id,
    score,
    amount,
    payload,
    created_at,
    updated_at
)
SELECT
    1 + (gs % 20000),
    CASE
        WHEN gs % 5000 = 0 THEN 42
        ELSE 100 + (gs % 400)
    END,
    1 + (gs % 200),
    CASE
        WHEN gs % 97 = 0 THEN 920 + (gs % 80)
        ELSE 100 + (gs % 650)
    END,
    round(((gs % 1000) * 1.07)::numeric, 2),
    repeat(md5(gs::text), 2),
    timestamp '2024-01-01 00:00:00' + ((gs % 2880) || ' minutes')::interval,
    timestamp '2024-01-01 00:00:00' + ((gs % 2880) || ' minutes')::interval
FROM generate_series(1, 500000) AS gs;

CREATE INDEX idx_fact_events_tenant_score ON fact_events (tenant_id, score);
CREATE INDEX idx_fact_events_account ON fact_events (account_id);
CREATE INDEX idx_fact_events_created_at ON fact_events (created_at);

ALTER TABLE fact_events ALTER COLUMN tenant_id SET STATISTICS 2000;
ALTER TABLE fact_events ALTER COLUMN score SET STATISTICS 2000;
ALTER TABLE fact_events ALTER COLUMN category_id SET STATISTICS 1000;

ANALYZE dim_accounts;
ANALYZE fact_events;
