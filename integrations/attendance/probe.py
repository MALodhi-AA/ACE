"""Attendance database probe (v0.5.2): what is in the attendance bot's MariaDB?

Prints the databases ACE's read-only login can see, their tables, columns (name and
type), number of rows and - for date/time columns - the earliest and latest value.
It never prints names, messages or other personal values.

    docker compose exec ace python -m integrations.attendance.probe
    docker compose exec ace python -m integrations.attendance.probe --db attendance

Needs ATT_DB_HOST, ATT_DB_PORT, ATT_DB_USER, ATT_DB_PASSWORD (and optionally ATT_DB_NAME) in .env.
"""
from __future__ import annotations

import argparse
import sys

from integrations.attendance.db import AttendanceDB, configured, settings_from_env

SYSTEM_DBS = {"information_schema", "mysql", "performance_schema", "sys"}
TIME_TYPES = {"date", "datetime", "timestamp", "time"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="only this database")
    ap.add_argument("--errors", action="store_true", help="summary of the task bot's failed chat deliveries")
    args = ap.parse_args()
    cfg = settings_from_env()
    if not configured():
        print("Set ATT_DB_HOST, ATT_DB_PORT, ATT_DB_USER and ATT_DB_PASSWORD in .env, then `docker compose up -d`.")
        return 2
    print(f"Attendance DB: {cfg['host']}:{cfg['port']} as {cfg['user']}")
    db = AttendanceDB(database=None)
    try:
        db.connect()
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        hint = ""
        if "1045" in msg:
            hint = " - wrong user/password, or the user is not allowed from this server's IP (192.168.2.120)"
        elif "2003" in msg or "timed out" in msg.lower():
            hint = (" - cannot reach the database: check 'Enable TCP/IP connection' and the port in MariaDB 10, "
                    "and the DS723+ firewall")
        print(f"CONNECTION FAILED: {msg}{hint}")
        return 1
    print("CONNECTED (read-only session)")
    if args.errors:
        from integrations.attendance.reader import Bot
        f = Bot(db=AttendanceDB()).delivery_failures()
        print(f"\nFailed chat deliveries: {f['hour']:,} in the last hour, {f['day']:,} in 24 hours")
        for r in f["top"]:
            print(f"  {r['n']:>8,} x {r['source'] or '?'}: {(r['error'] or '').strip()}")
        for r in f["channels"]:
            print(f"  channel {r['channel'] or '?'}: {r['n']:,}")
        return 0
    for g in db.query("SHOW GRANTS"):
        grant = list(g.values())[0]
        print(f"  grant: {grant.split(' IDENTIFIED')[0]}")
    dbs = [list(r.values())[0] for r in db.query("SHOW DATABASES")]
    dbs = [d for d in dbs if d not in SYSTEM_DBS and (not args.db or d == args.db)]
    print(f"\nDatabases visible: {', '.join(dbs) or '(none)'}")
    for name in dbs:
        tables = db.query("SELECT table_name AS t, table_rows AS n, table_type AS k FROM information_schema.tables "
                          "WHERE table_schema=%s ORDER BY table_name", (name,))
        print(f"\n=== database {name}: {len(tables)} tables/views ===")
        for t in tables:
            tname = t["t"]
            try:
                count = db.query(f"SELECT COUNT(*) AS c FROM `{name}`.`{tname}`")[0]["c"]
            except Exception:  # noqa: BLE001
                count = t["n"]
            print(f"\n- {tname} ({'view' if 'VIEW' in (t['k'] or '') else 'table'}, {count} rows)")
            cols = db.query("SELECT column_name AS c, column_type AS ty, data_type AS dt, column_key AS k "
                            "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                            "ORDER BY ordinal_position", (name, tname))
            for c in cols:
                extra = ""
                if c["dt"] in TIME_TYPES and count:
                    try:
                        r = db.query(f"SELECT MIN(`{c['c']}`) AS lo, MAX(`{c['c']}`) AS hi FROM `{name}`.`{tname}`")[0]
                        extra = f"  [{r['lo']} .. {r['hi']}]"
                    except Exception:  # noqa: BLE001
                        pass
                elif c["dt"] in ("enum", "set"):
                    extra = f"  {c['ty']}"
                key = " PK" if c["k"] == "PRI" else (" idx" if c["k"] else "")
                print(f"    {c['c']:32} {c['ty']:24}{key}{extra}")
    db.close()
    print("\nNo personal values were printed. Send this output to Claude.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
