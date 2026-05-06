from __future__ import annotations

from typing import Dict, List


def prepare_support_assets(cursor, database: str) -> Dict[str, List[str]]:
    cursor.execute(f"USE `{database}`")
    created = []
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_order_by_support AS
        SELECT ol_w_id, ol_d_id, ol_o_id, ol_i_id, ol_quantity, ol_amount, ol_dist_info
        FROM order_line
        """
    )
    created.append("agent_order_by_support")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_group_by_support AS
        SELECT ol_w_id, ol_i_id, ol_quantity, ol_amount
        FROM order_line
        """
    )
    created.append("agent_group_by_support")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_large_scan_support AS
        SELECT c_w_id, c_d_id, c_id, c_last, c_credit, c_balance, c_delivery_cnt
        FROM customer
        """
    )
    created.append("agent_large_scan_support")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_implicit_conversion_support (
            id INT PRIMARY KEY AUTO_INCREMENT,
            customer_id_varchar VARCHAR(32) NOT NULL,
            customer_id_int INT NOT NULL,
            amount_text VARCHAR(32) NOT NULL,
            KEY idx_customer_id_varchar (customer_id_varchar),
            KEY idx_customer_id_int (customer_id_int)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute("SELECT COUNT(*) FROM agent_implicit_conversion_support")
    if int(cursor.fetchone()[0]) == 0:
        cursor.execute(
            """
            INSERT INTO agent_implicit_conversion_support(customer_id_varchar, customer_id_int, amount_text)
            SELECT CAST(c_id AS CHAR), c_id, CAST(c_balance AS CHAR)
            FROM customer
            LIMIT 50000
            """
        )
    created.append("agent_implicit_conversion_support")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_excessive_index AS
        SELECT o_id, o_c_id, o_carrier_id, o_ol_cnt
        FROM orders
        LIMIT 50000
        """
    )
    for statement in [
        "CREATE INDEX idx_agent_excessive_index_c1 ON agent_excessive_index(o_c_id)",
        "CREATE INDEX idx_agent_excessive_index_c2 ON agent_excessive_index(o_c_id, o_carrier_id)",
        "CREATE INDEX idx_agent_excessive_index_c3 ON agent_excessive_index(o_carrier_id, o_ol_cnt)",
    ]:
        try:
            cursor.execute(statement)
        except Exception:
            pass
    created.append("agent_excessive_index")
    return {"created_tables": created}
