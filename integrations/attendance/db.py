"""Read-only connection to the attendance bot's MariaDB (on the DS723+).

ACE uses its own login (`ace_ro`, SELECT only). On top of that, every session is set
to READ ONLY and only SELECT / SHOW / DESCRIBE statements are allowed from here.
"""
from __future__ import annotations

import os
import re

READ_ONLY_SQL = re.compile(r"^\s*(select|show|describe|desc|explain)\b", re.IGNORECASE)


def settings_from_env() -> dict:
    return {
        "host": os.getenv("ATT_DB_HOST", "").strip(),
        "port": int(os.getenv("ATT_DB_PORT", "3306") or 3306),
        "user": os.getenv("ATT_DB_USER", "").strip(),
        "password": os.getenv("ATT_DB_PASSWORD", ""),
        "database": os.getenv("ATT_DB_NAME", "").strip() or None,
    }


def configured() -> bool:
    s = settings_from_env()
    return bool(s["host"] and s["user"] and s["password"])


class AttendanceDB:
    def __init__(self, **kw) -> None:
        self.cfg = {**settings_from_env(), **kw}
        self._conn = None

    def connect(self):
        import pymysql

        if self._conn is None:
            self._conn = pymysql.connect(
                host=self.cfg["host"], port=self.cfg["port"], user=self.cfg["user"],
                password=self.cfg["password"], database=self.cfg["database"], connect_timeout=10,
                read_timeout=30, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=True)
            with self._conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
        return self._conn

    def query(self, sql: str, args=None) -> list[dict]:
        if not READ_ONLY_SQL.match(sql):
            raise PermissionError("ACE only reads from the attendance database")
        conn = self.connect()
        conn.ping(reconnect=True)
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
