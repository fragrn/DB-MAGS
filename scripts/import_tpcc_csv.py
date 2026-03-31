#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.env_loader import load_dotenv_files


TABLE_SPECS = [
    {
        "csv": "warehouse.csv",
        "table": "warehouse",
        "ddl": """
            CREATE TABLE warehouse (
                w_id SMALLINT NOT NULL,
                w_name VARCHAR(10) NOT NULL,
                w_street_1 VARCHAR(20) NOT NULL,
                w_street_2 VARCHAR(20) NOT NULL,
                w_city VARCHAR(20) NOT NULL,
                w_state CHAR(2) NOT NULL,
                w_zip CHAR(9) NOT NULL,
                w_tax DECIMAL(4,4) NOT NULL,
                w_ytd DECIMAL(12,2) NOT NULL,
                PRIMARY KEY (w_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "load_sql": """
            LOAD DATA LOCAL INFILE %s
            INTO TABLE warehouse
            FIELDS TERMINATED BY ','
            LINES TERMINATED BY '\n'
            (w_id, w_name, w_street_1, w_street_2, w_city, w_state, w_zip, @w_tax, @w_ytd)
            SET w_tax = @w_tax, w_ytd = @w_ytd
        """,
    },
    {
        "csv": "district.csv",
        "table": "district",
        "ddl": """
            CREATE TABLE district (
                d_id TINYINT NOT NULL,
                d_w_id SMALLINT NOT NULL,
                d_name VARCHAR(10) NOT NULL,
                d_street_1 VARCHAR(20) NOT NULL,
                d_street_2 VARCHAR(20) NOT NULL,
                d_city VARCHAR(20) NOT NULL,
                d_state CHAR(2) NOT NULL,
                d_zip CHAR(9) NOT NULL,
                d_tax DECIMAL(4,4) NOT NULL,
                d_ytd DECIMAL(12,2) NOT NULL,
                d_next_o_id INT NOT NULL,
                PRIMARY KEY (d_w_id, d_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "load_sql": """
            LOAD DATA LOCAL INFILE %s
            INTO TABLE district
            FIELDS TERMINATED BY ','
            LINES TERMINATED BY '\n'
            (d_id, d_w_id, d_name, d_street_1, d_street_2, d_city, d_state, d_zip, @d_tax, @d_ytd, d_next_o_id)
            SET d_tax = @d_tax, d_ytd = @d_ytd
        """,
    },
    {
        "csv": "item.csv",
        "table": "item",
        "ddl": """
            CREATE TABLE item (
                i_id INT NOT NULL,
                i_im_id INT NOT NULL,
                i_name VARCHAR(24) NOT NULL,
                i_price DECIMAL(5,2) NOT NULL,
                i_data VARCHAR(50) NOT NULL,
                PRIMARY KEY (i_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "load_sql": """
            LOAD DATA LOCAL INFILE %s
            INTO TABLE item
            FIELDS TERMINATED BY ','
            LINES TERMINATED BY '\n'
            (i_id, i_im_id, i_name, @i_price, i_data)
            SET i_price = @i_price
        """,
    },
    {
        "csv": "customer.csv",
        "table": "customer",
        "ddl": """
            CREATE TABLE customer (
                c_id INT NOT NULL,
                c_d_id TINYINT NOT NULL,
                c_w_id SMALLINT NOT NULL,
                c_first VARCHAR(16) NOT NULL,
                c_middle CHAR(2) NOT NULL,
                c_last VARCHAR(16) NOT NULL,
                c_street_1 VARCHAR(20) NOT NULL,
                c_street_2 VARCHAR(20) NOT NULL,
                c_city VARCHAR(20) NOT NULL,
                c_state CHAR(2) NOT NULL,
                c_zip CHAR(9) NOT NULL,
                c_phone CHAR(16) NOT NULL,
                c_since DATETIME NOT NULL,
                c_credit CHAR(2) NOT NULL,
                c_credit_lim DECIMAL(12,2) NOT NULL,
                c_discount DECIMAL(4,4) NOT NULL,
                c_balance DECIMAL(12,2) NOT NULL,
                c_ytd_payment DECIMAL(12,2) NOT NULL,
                c_payment_cnt SMALLINT NOT NULL,
                c_delivery_cnt SMALLINT NOT NULL,
                c_data VARCHAR(500) NOT NULL,
                PRIMARY KEY (c_w_id, c_d_id, c_id),
                KEY idx_customer_last (c_w_id, c_d_id, c_last, c_first)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "load_sql": """
            LOAD DATA LOCAL INFILE %s
            INTO TABLE customer
            FIELDS TERMINATED BY ','
            LINES TERMINATED BY '\n'
            (c_id, c_d_id, c_w_id, c_first, c_middle, c_last, c_street_1, c_street_2, c_city, c_state,
             c_zip, c_phone, @c_since, c_credit, @c_credit_lim, @c_discount, @c_balance, @c_ytd_payment,
             c_payment_cnt, c_delivery_cnt, c_data)
            SET c_since = FROM_UNIXTIME(@c_since),
                c_credit_lim = @c_credit_lim,
                c_discount = @c_discount,
                c_balance = @c_balance,
                c_ytd_payment = @c_ytd_payment
        """,
    },
    {
        "csv": "history.csv",
        "table": "history",
        "ddl": """
            CREATE TABLE history (
                h_c_id INT NOT NULL,
                h_c_d_id TINYINT NOT NULL,
                h_c_w_id SMALLINT NOT NULL,
                h_d_id TINYINT NOT NULL,
                h_w_id SMALLINT NOT NULL,
                h_date DATETIME NOT NULL,
                h_amount DECIMAL(6,2) NOT NULL,
                h_data VARCHAR(24) NOT NULL,
                KEY idx_history_customer (h_c_w_id, h_c_d_id, h_c_id),
                KEY idx_history_warehouse (h_w_id, h_d_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "load_sql": """
            LOAD DATA LOCAL INFILE %s
            INTO TABLE history
            FIELDS TERMINATED BY ','
            LINES TERMINATED BY '\n'
            (h_c_id, h_c_d_id, h_c_w_id, h_d_id, h_w_id, @h_date, @h_amount, h_data)
            SET h_date = FROM_UNIXTIME(@h_date),
                h_amount = @h_amount
        """,
    },
    {
        "csv": "stock.csv",
        "table": "stock",
        "ddl": """
            CREATE TABLE stock (
                s_i_id INT NOT NULL,
                s_w_id SMALLINT NOT NULL,
                s_quantity SMALLINT NOT NULL,
                s_dist_01 CHAR(24) NOT NULL,
                s_dist_02 CHAR(24) NOT NULL,
                s_dist_03 CHAR(24) NOT NULL,
                s_dist_04 CHAR(24) NOT NULL,
                s_dist_05 CHAR(24) NOT NULL,
                s_dist_06 CHAR(24) NOT NULL,
                s_dist_07 CHAR(24) NOT NULL,
                s_dist_08 CHAR(24) NOT NULL,
                s_dist_09 CHAR(24) NOT NULL,
                s_dist_10 CHAR(24) NOT NULL,
                s_ytd INT NOT NULL,
                s_order_cnt INT NOT NULL,
                s_remote_cnt INT NOT NULL,
                s_data VARCHAR(50) NOT NULL,
                PRIMARY KEY (s_w_id, s_i_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "load_sql": """
            LOAD DATA LOCAL INFILE %s
            INTO TABLE stock
            FIELDS TERMINATED BY ','
            LINES TERMINATED BY '\n'
            (s_i_id, s_w_id, s_quantity, s_dist_01, s_dist_02, s_dist_03, s_dist_04, s_dist_05,
             s_dist_06, s_dist_07, s_dist_08, s_dist_09, s_dist_10, s_ytd, s_order_cnt, s_remote_cnt, s_data)
        """,
    },
    {
        "csv": "order.csv",
        "table": "orders",
        "ddl": """
            CREATE TABLE orders (
                o_id INT NOT NULL,
                o_d_id TINYINT NOT NULL,
                o_w_id SMALLINT NOT NULL,
                o_c_id INT NOT NULL,
                o_entry_d DATETIME NOT NULL,
                o_carrier_id TINYINT NULL,
                o_ol_cnt TINYINT NOT NULL,
                o_all_local TINYINT NOT NULL,
                PRIMARY KEY (o_w_id, o_d_id, o_id),
                KEY idx_orders_customer (o_w_id, o_d_id, o_c_id, o_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "load_sql": """
            LOAD DATA LOCAL INFILE %s
            INTO TABLE orders
            FIELDS TERMINATED BY ','
            LINES TERMINATED BY '\n'
            (o_id, o_d_id, o_w_id, o_c_id, @o_entry_d, @o_carrier_id, o_ol_cnt, o_all_local)
            SET o_entry_d = FROM_UNIXTIME(@o_entry_d),
                o_carrier_id = NULLIF(@o_carrier_id, 'null')
        """,
    },
    {
        "csv": "new_order.csv",
        "table": "new_orders",
        "ddl": """
            CREATE TABLE new_orders (
                no_o_id INT NOT NULL,
                no_d_id TINYINT NOT NULL,
                no_w_id SMALLINT NOT NULL,
                PRIMARY KEY (no_w_id, no_d_id, no_o_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "load_sql": """
            LOAD DATA LOCAL INFILE %s
            INTO TABLE new_orders
            FIELDS TERMINATED BY ','
            LINES TERMINATED BY '\n'
            (no_o_id, no_d_id, no_w_id)
        """,
    },
    {
        "csv": "order_line.csv",
        "table": "order_line",
        "ddl": """
            CREATE TABLE order_line (
                ol_o_id INT NOT NULL,
                ol_d_id TINYINT NOT NULL,
                ol_w_id SMALLINT NOT NULL,
                ol_number TINYINT NOT NULL,
                ol_i_id INT NOT NULL,
                ol_supply_w_id SMALLINT NOT NULL,
                ol_delivery_d DATETIME NULL,
                ol_quantity TINYINT NOT NULL,
                ol_amount DECIMAL(6,2) NOT NULL,
                ol_dist_info CHAR(24) NOT NULL,
                PRIMARY KEY (ol_w_id, ol_d_id, ol_o_id, ol_number)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "load_sql": """
            LOAD DATA LOCAL INFILE %s
            INTO TABLE order_line
            FIELDS TERMINATED BY ','
            LINES TERMINATED BY '\n'
            (ol_o_id, ol_d_id, ol_w_id, ol_number, ol_i_id, ol_supply_w_id, @ol_delivery_d, ol_quantity, @ol_amount, ol_dist_info)
            SET ol_delivery_d = CASE
                    WHEN LOWER(@ol_delivery_d) = 'null' OR @ol_delivery_d = '' THEN NULL
                    ELSE FROM_UNIXTIME(@ol_delivery_d)
                END,
                ol_amount = @ol_amount
        """,
    },
]


def parse_args():
    parser = argparse.ArgumentParser(description="Import TPCC CSV files into MySQL and clone to a copy database.")
    parser.add_argument("--csv-dir", default=".tools/tpcc-generator/my_tpcc_input")
    parser.add_argument("--target-db", default="dbmags_agent_lab")
    parser.add_argument("--copy-db", default="dbmags_agent_lab_copy")
    parser.add_argument("--mysql-host", default=None)
    parser.add_argument("--mysql-port", type=int, default=None)
    parser.add_argument("--mysql-user", default=None)
    parser.add_argument("--mysql-password", default=None)
    return parser.parse_args()


def env_default(name, fallback):
    value = os.getenv(name)
    return fallback if value is None else value


def connect(database=None):
    return pymysql.connect(
        host=env_default("DBMAGS_MYSQL_HOST", "127.0.0.1"),
        port=int(env_default("DBMAGS_MYSQL_PORT", "3306")),
        user=env_default("DBMAGS_MYSQL_USER", "root"),
        passwd=env_default("DBMAGS_MYSQL_PASSWORD", ""),
        database=database,
        charset="utf8mb4",
        autocommit=True,
        local_infile=True,
    )


def count_lines(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1024 * 1024), b""))


def file_summary(csv_dir: Path):
    summary = []
    for spec in TABLE_SPECS:
        path = csv_dir / spec["csv"]
        if not path.exists() or path.stat().st_size == 0:
            raise FileNotFoundError(f"missing or empty csv: {path}")
        summary.append(
            {
                "csv": spec["csv"],
                "table": spec["table"],
                "bytes": path.stat().st_size,
                "rows": count_lines(path),
            }
        )
    return summary


def ensure_database(cursor, name: str):
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")


def ensure_local_infile_enabled(cursor):
    cursor.execute("SHOW VARIABLES LIKE 'local_infile'")
    row = cursor.fetchone()
    if row and str(row[1]).upper() == "OFF":
        cursor.execute("SET GLOBAL local_infile = 1")


def recreate_target_schema(cursor, database: str):
    ensure_database(cursor, database)
    cursor.execute(f"USE `{database}`")
    for spec in reversed(TABLE_SPECS):
        cursor.execute(f"DROP TABLE IF EXISTS `{spec['table']}`")
    for spec in TABLE_SPECS:
        cursor.execute(spec["ddl"])


def import_tables(cursor, database: str, csv_dir: Path):
    results = []
    cursor.execute(f"USE `{database}`")
    for spec in TABLE_SPECS:
        start = time.time()
        csv_path = str((csv_dir / spec["csv"]).resolve())
        cursor.execute(spec["load_sql"], (csv_path,))
        cursor.execute(f"SELECT COUNT(*) FROM `{spec['table']}`")
        imported_rows = cursor.fetchone()[0]
        results.append(
            {
                "table": spec["table"],
                "csv": spec["csv"],
                "rows_imported": imported_rows,
                "seconds": round(time.time() - start, 3),
            }
        )
    return results


def clone_database(cursor, source: str, target: str):
    ensure_database(cursor, target)
    cursor.execute(f"USE `{target}`")
    for spec in reversed(TABLE_SPECS):
        cursor.execute(f"DROP TABLE IF EXISTS `{spec['table']}`")

    results = []
    for spec in TABLE_SPECS:
        start = time.time()
        table = spec["table"]
        cursor.execute(f"CREATE TABLE `{target}`.`{table}` LIKE `{source}`.`{table}`")
        cursor.execute(f"INSERT INTO `{target}`.`{table}` SELECT * FROM `{source}`.`{table}`")
        cursor.execute(f"SELECT COUNT(*) FROM `{target}`.`{table}`")
        copied_rows = cursor.fetchone()[0]
        results.append(
            {
                "table": table,
                "rows_copied": copied_rows,
                "seconds": round(time.time() - start, 3),
            }
        )
    return results


def compatibility_checks(cursor, database: str):
    cursor.execute(f"USE `{database}`")
    checks = {}
    for name, sql in [
        ("orders_count", "SELECT COUNT(*) FROM orders"),
        ("order_line_count", "SELECT COUNT(*) FROM order_line"),
        ("customer_zip_sample", "SELECT c_zip FROM customer LIMIT 10"),
        ("district_next_order_sample", "SELECT d_next_o_id FROM district LIMIT 10"),
        ("stock_sample", "SELECT s_dist_01, s_order_cnt FROM stock LIMIT 10"),
    ]:
        cursor.execute(sql)
        checks[name] = cursor.fetchall()
    return checks


def main():
    args = parse_args()
    load_dotenv_files()

    if args.mysql_host:
        os.environ["DBMAGS_MYSQL_HOST"] = args.mysql_host
    if args.mysql_port:
        os.environ["DBMAGS_MYSQL_PORT"] = str(args.mysql_port)
    if args.mysql_user:
        os.environ["DBMAGS_MYSQL_USER"] = args.mysql_user
    if args.mysql_password is not None:
        os.environ["DBMAGS_MYSQL_PASSWORD"] = args.mysql_password

    csv_dir = Path(args.csv_dir)
    summaries = file_summary(csv_dir)

    with connect() as conn:
        with conn.cursor() as cursor:
            ensure_local_infile_enabled(cursor)

    with connect() as conn:
        with conn.cursor() as cursor:
            recreate_target_schema(cursor, args.target_db)
            imported = import_tables(cursor, args.target_db, csv_dir)
            copied = clone_database(cursor, args.target_db, args.copy_db)
            compatibility = compatibility_checks(cursor, args.target_db)

    print(
        json.dumps(
            {
                "csv_dir": str(csv_dir.resolve()),
                "target_db": args.target_db,
                "copy_db": args.copy_db,
                "files": summaries,
                "imported": imported,
                "copied": copied,
                "compatibility_checks": compatibility,
                "time_conversions": {
                    "customer.c_since": "FROM_UNIXTIME(epoch_seconds)",
                    "history.h_date": "FROM_UNIXTIME(epoch_seconds)",
                    "orders.o_entry_d": "FROM_UNIXTIME(epoch_seconds)",
                    "order_line.ol_delivery_d": "null -> NULL, else FROM_UNIXTIME(epoch_seconds)",
                },
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
