import pymysql
from sshtunnel import SSHTunnelForwarder

try:
    from experiments.causal_graph_agent.config import MySQLConfig
except Exception:
    MySQLConfig = None


class Database:
    def __init__(self):
        cfg = MySQLConfig.from_env() if MySQLConfig is not None else None
        # 服务器连接配置
        self.server_address = cfg.server_address if cfg and cfg.server_address else '113.31.103.14'
        self.server_password = cfg.server_password if cfg and cfg.server_password else 'Meituan312'
        self.server_username = cfg.server_username if cfg and cfg.server_username else 'root'

        # mysql数据连接配置
        self.mysql_user = cfg.user if cfg else 'root'
        self.mysql_password = cfg.password if cfg else 'Meituan312'
        self.mysql_db = cfg.database if cfg else 'tpcc10_test'
        self.mysql_host = cfg.host if cfg else '127.0.0.1'
        self.mysql_port = cfg.port if cfg else 3306

    def connection(self):
        # 连接至服务器
        server = SSHTunnelForwarder(
            # 指定ssh登录的跳转机的address
            ssh_address_or_host=(self.server_address, 22),
            # 设置密钥
            # ssh_pkey = private_key,
            # 如果是通过密码访问，可以把下面注释打开，将密钥注释即可。
            ssh_password=self.server_password,
            # 设置用户
            ssh_username=self.server_username,
            # 设置数据库服务地址及端口
            remote_bind_address=('127.0.0.1', 3306))
        server.start()
        # 创建连接
        conn = pymysql.connect(host='127.0.0.1', port=server.local_bind_port, user=self.mysql_user,
                               passwd=self.mysql_password,
                               db=self.mysql_db, charset='utf8')
        # conn = mysql.connector.connect(database=self.mysql_db,
        #                                user=self.mysql_user,
        #                                password=self.mysql_password,
        #                                host=self.mysql_host,
        #                                # 因为上面没有设置 local_bind_address,所以这里必须是127.0.0.1,如果设置了，取设置的值就行了。
        #                                port=server.local_bind_port,
        #                                autocommit=True)  # 这里端口也一样，上面的server可以设置，没设置取这个就行了
        # 创建游标
        cur = conn.cursor()
        return conn, cur

    def connection1(self):
        conn = pymysql.connect(host=self.mysql_host, port=self.mysql_port, user=self.mysql_user,
                               passwd=self.mysql_password,
                                # charset='utf8')
                               db=self.mysql_db, charset='utf8')
        cur = conn.cursor()
        return conn, cur

    def connection2(self):
        conn = pymysql.connect(host=self.mysql_host, port=self.mysql_port, user=self.mysql_user,
                               passwd=self.mysql_password,
                                # charset='utf8')
                               db=self.mysql_db, charset='utf8')
        cur = conn.cursor()
        return conn, cur
