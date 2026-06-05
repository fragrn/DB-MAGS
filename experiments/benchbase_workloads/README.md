# BenchBase PostgreSQL Workloads

This directory contains BenchBase configs used to load PostgreSQL benchmark datasets for anomaly-chain experiments.

## Loaded Datasets

The datasets are loaded into the dedicated PostgreSQL container `dbmags-benchbase-postgres` on the Docker network `benchbase_postgres_env_default`.

| Workload | Database | Scale | Intended chains |
|---|---|---:|---|
| TPC-C | `benchbase_tpcc_10w` | 10 warehouses | traffic surge, lock contention, missing index, timeout |
| TPC-H | `benchbase_tpch_sf01` | SF 0.1 | stale stats, poor plan, sort/hash spill, temp IO |

Connection from host:

```bash
PGPASSWORD=postgres psql -h 127.0.0.1 -p 55433 -U postgres -d benchbase_tpcc_10w
PGPASSWORD=postgres psql -h 127.0.0.1 -p 55433 -U postgres -d benchbase_tpch_sf01
```

Connection from a container on the same Docker network:

```text
host: dbmags-benchbase-postgres
port: 5432
user: postgres
password: postgres
```

## Environment

Start the dedicated PostgreSQL environment:

```bash
docker compose -f experiments/benchbase_postgres_env/docker-compose.yml up -d
```

Load both datasets:

```bash
bash experiments/benchbase_workloads/load_postgres_workloads.sh
```

This environment uses its own Docker volume and is intentionally separate from `experiments/postgres_dead_tuples_chain`, whose agent runs may delete and recreate their volume.

## Load Commands

TPC-C:

```bash
docker run --rm \
  --network benchbase_postgres_env_default \
  -v "$PWD/experiments/benchbase_workloads:/configs" \
  -v "$PWD/experiments/benchbase_workloads/results:/benchbase/results" \
  benchbase.azurecr.io/benchbase-postgres \
  -b tpcc -c /configs/postgres_tpcc_10w.xml \
  --create=true --load=true --execute=false
```

TPC-H:

```bash
docker run --rm \
  --network benchbase_postgres_env_default \
  -v "$PWD/experiments/benchbase_workloads:/configs" \
  -v "$PWD/experiments/benchbase_workloads/results:/benchbase/results" \
  benchbase.azurecr.io/benchbase-postgres \
  -b tpch -c /configs/postgres_tpch_sf0.1.xml \
  --create=true --load=true --execute=false
```

## Verified Row Counts

TPC-C 10 warehouses:

```text
warehouse: 10
district: 100
customer: 300000
history: 300000
item: 100000
stock: 1000000
orders: 300000
new_order: 90000
order_line: 3000644
```

TPC-H SF 0.1:

```text
region: 5
nation: 25
supplier: 1000
customer: 15000
part: 20000
partsupp: 80000
orders: 150000
lineitem: 600572
```
