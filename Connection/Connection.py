import os

import pymysql
from sshtunnel import SSHTunnelForwarder


class Database:
    def __init__(
        self,
        server_address=None,
        server_password=None,
        server_username=None,
        mysql_user=None,
        mysql_password=None,
        mysql_db=None,
        mysql_host=None,
        mysql_port=None,
    ):
        # Existing defaults stay in place so legacy scripts keep working.
        self.server_address = server_address or os.getenv("DBMAGS_SERVER_ADDRESS", "127.0.0.1")
        self.server_password = server_password or os.getenv("DBMAGS_SERVER_PASSWORD", "")
        self.server_username = server_username or os.getenv("DBMAGS_SERVER_USERNAME", "root")

        self.mysql_user = mysql_user or os.getenv("DBMAGS_MYSQL_USER", "root")
        self.mysql_password = mysql_password if mysql_password is not None else os.getenv("DBMAGS_MYSQL_PASSWORD", "")
        self.mysql_db = mysql_db or os.getenv("DBMAGS_MYSQL_DB", "tpcc10_test")
        self.mysql_host = mysql_host or os.getenv("DBMAGS_MYSQL_HOST", "127.0.0.1")
        self.mysql_port = int(mysql_port or os.getenv("DBMAGS_MYSQL_PORT", "3306"))

    def _connect(self, host, port):
        conn = pymysql.connect(
            host=host,
            port=port,
            user=self.mysql_user,
            passwd=self.mysql_password,
            db=self.mysql_db,
            charset="utf8",
        )
        cur = conn.cursor()
        return conn, cur

    def connection(self):
        server = SSHTunnelForwarder(
            ssh_address_or_host=(self.server_address, 22),
            ssh_password=self.server_password,
            ssh_username=self.server_username,
            remote_bind_address=(self.mysql_host, self.mysql_port),
        )
        server.start()
        return self._connect("127.0.0.1", server.local_bind_port)

    def connection1(self):
        return self._connect(self.mysql_host, self.mysql_port)

    def connection2(self):
        return self._connect(self.mysql_host, self.mysql_port)

    def direct_connection(self):
        return self._connect(self.mysql_host, self.mysql_port)
