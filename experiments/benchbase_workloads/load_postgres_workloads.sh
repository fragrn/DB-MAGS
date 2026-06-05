#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/experiments/benchbase_postgres_env/docker-compose.yml"
RESULTS_DIR="$SCRIPT_DIR/results"
NETWORK="benchbase_postgres_env_default"
PG_CONTAINER="dbmags-benchbase-postgres"
BENCHBASE_IMAGE="${BENCHBASE_IMAGE:-benchbase.azurecr.io/benchbase-postgres}"

mkdir -p "$RESULTS_DIR"

run_psql() {
  docker exec "$PG_CONTAINER" env PGPASSWORD=postgres psql -U postgres -d postgres -v ON_ERROR_STOP=1 "$@"
}

create_database() {
  local db_name="$1"
  run_psql \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${db_name}';" \
    -c "DROP DATABASE IF EXISTS ${db_name};" \
    -c "CREATE DATABASE ${db_name};"
}

load_workload() {
  local bench="$1"
  local config="$2"
  docker run --rm \
    --network "$NETWORK" \
    -v "$SCRIPT_DIR:/configs" \
    -v "$RESULTS_DIR:/benchbase/results" \
    "$BENCHBASE_IMAGE" \
    -b "$bench" -c "/configs/$config" \
    --create=true --load=true --execute=false
}

show_counts() {
  echo
  echo "TPC-C row counts:"
  docker exec "$PG_CONTAINER" env PGPASSWORD=postgres psql -U postgres -d benchbase_tpcc_10w -A -F $'\t' -c "
    SELECT 'warehouse', count(*) FROM warehouse
    UNION ALL SELECT 'district', count(*) FROM district
    UNION ALL SELECT 'customer', count(*) FROM customer
    UNION ALL SELECT 'history', count(*) FROM history
    UNION ALL SELECT 'item', count(*) FROM item
    UNION ALL SELECT 'stock', count(*) FROM stock
    UNION ALL SELECT 'orders', count(*) FROM oorder
    UNION ALL SELECT 'new_order', count(*) FROM new_order
    UNION ALL SELECT 'order_line', count(*) FROM order_line
    ORDER BY 1;"

  echo
  echo "TPC-H row counts:"
  docker exec "$PG_CONTAINER" env PGPASSWORD=postgres psql -U postgres -d benchbase_tpch_sf01 -A -F $'\t' -c "
    SELECT 'region', count(*) FROM region
    UNION ALL SELECT 'nation', count(*) FROM nation
    UNION ALL SELECT 'supplier', count(*) FROM supplier
    UNION ALL SELECT 'customer', count(*) FROM customer
    UNION ALL SELECT 'part', count(*) FROM part
    UNION ALL SELECT 'partsupp', count(*) FROM partsupp
    UNION ALL SELECT 'orders', count(*) FROM orders
    UNION ALL SELECT 'lineitem', count(*) FROM lineitem
    ORDER BY 1;"
}

docker compose -f "$COMPOSE_FILE" up -d

echo "Waiting for $PG_CONTAINER..."
until docker exec "$PG_CONTAINER" pg_isready -U postgres -d postgres >/dev/null 2>&1; do
  sleep 2
done

create_database benchbase_tpcc_10w
create_database benchbase_tpch_sf01

load_workload tpcc postgres_tpcc_10w.xml
load_workload tpch postgres_tpch_sf0.1.xml

show_counts
