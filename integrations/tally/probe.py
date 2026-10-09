"""Tally probe (v0.8.0): can ACE read TallyPrime? Read-only.

    docker exec ace python -m integrations.tally.probe
        connection + the companies open in Tally
    docker exec ace python -m integrations.tally.probe --company "Mara Trading LLC"
        one company: number of ledgers and groups, total debits / credits, last entry date
        (no ledger names or balances are printed unless you add --show)
    docker exec ace python -m integrations.tally.probe --company "..." --report "Trial Balance" --raw
        saves Tally's raw XML for a report to state/tally-probe/ (to check the format)

Options: --from 2026-01-01 --to 2026-09-30 (default: Tally's current period)
Settings (.env): TALLY_URL (default http://host.docker.internal:9000), TALLY_TIMEOUT (seconds).
"""
from __future__ import annotations

import argparse
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

from integrations.tally.client import Tally, TallyError, dr_cr, settings_from_env


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _pick(companies: list[dict], wanted: str) -> str | None:
    low = wanted.strip().lower()
    exact = [c["name"] for c in companies if c["name"].lower() == low]
    if exact:
        return exact[0]
    part = [c["name"] for c in companies if low in c["name"].lower()]
    if len(part) == 1:
        return part[0]
    if part:
        print(f"'{wanted}' matches {len(part)} companies - be more specific:")
        for p in part:
            print(f"  {p}")
    else:
        print(f"No open company matches '{wanted}'.")
    return None


def main(argv: list[str] | None = None, tally: Tally | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--company")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--show", action="store_true", help="also print the 15 largest ledger balances")
    ap.add_argument("--report", help="a Tally report name, e.g. 'Trial Balance'")
    ap.add_argument("--raw", action="store_true", help="save the report's raw XML to state/tally-probe/")
    ap.add_argument("--vouchers", action="store_true",
                    help="read the last 7 days of vouchers with their ledger lines (counts only)")
    ap.add_argument("--dates", action="store_true",
                    help="check whether Tally applies the period ACE asks for (default 1 Jan - 31 Dec 2024)")
    args = ap.parse_args(argv)
    frm, to = _d(args.frm), _d(args.to)

    t = tally or Tally()
    print(f"Tally: {settings_from_env()['url'] if tally is None else t.url} (read-only: Export requests only)")
    start = time.monotonic()
    try:
        companies = t.companies()
    except TallyError as exc:
        print(f"CONNECTION FAILED: {exc}")
        return 1
    print(f"CONNECTED in {time.monotonic() - start:.1f}s - {len(companies)} companies open")
    if not args.company:
        for c in sorted(companies, key=lambda c: c["name"].lower()):
            books = f"books from {c['books_from']:%d %b %Y}" if c["books_from"] else ""
            print(f"  {c['name']}  {books}")
        if not companies:
            print("  (none - open the companies in TallyPrime: Alt+F3 / Company > Select)")
        return 0

    name = _pick(companies, args.company)
    if not name:
        return 1
    print(f"\nCompany: {name}")
    try:
        if args.report:
            start = time.monotonic()
            root = t.report(name, args.report, frm, to)
            raw = ET.tostring(root, encoding="unicode")
            tags = sorted({el.tag for el in root.iter()})
            print(f"Report '{args.report}': {len(raw):,} characters in {time.monotonic() - start:.1f}s; "
                  f"tags: {', '.join(tags[:25])}{' ...' if len(tags) > 25 else ''}")
            if args.raw:
                out = Path("/data/state/tally-probe")
                out.mkdir(parents=True, exist_ok=True)
                f = out / f"{args.report.replace(' ', '_')}-{time.strftime('%Y%m%d-%H%M%S')}.xml"
                f.write_text(raw, encoding="utf-8")
                print(f"Saved to state/tally-probe/{f.name}")
            return 0

        if args.vouchers:
            end = to or date.today()
            start = time.monotonic()
            vs = t.vouchers(name, frm or end - timedelta(days=6), end)
            took = time.monotonic() - start
            lines = sum(len(v["lines"]) for v in vs)
            types = {}
            for v in vs:
                types[v["type"]] = types.get(v["type"], 0) + 1
            unbalanced = sum(1 for v in vs if v["lines"] and abs(sum(a for _, a in v["lines"])) > 0.01)
            print(f"Vouchers {(frm or end - timedelta(days=6)):%d %b} - {end:%d %b %Y}: {len(vs)} with {lines} ledger "
                  f"lines (read in {took:.1f}s); without lines: {sum(1 for v in vs if not v['lines'])}; "
                  f"lines not adding to zero: {unbalanced}")
            print("  by type: " + ", ".join(f"{k or '?'} {n}" for k, n in sorted(types.items())))
            return 0
        if args.dates:
            return _date_check(t, name, frm or date(2024, 1, 1), to or date(2024, 12, 31))

        start = time.monotonic()
        ledgers = t.ledgers(name, frm, to)
        took = time.monotonic() - start
        dr = sum(-l.closing for l in ledgers if l.closing < 0)
        cr = sum(l.closing for l in ledgers if l.closing > 0)
        groups = {l.parent for l in ledgers if l.parent}
        print(f"Ledgers: {len(ledgers)} in {len(groups)} groups (read in {took:.1f}s)")
        print(f"Closing balances: debits {dr:,.2f}, credits {cr:,.2f}, difference {dr_cr(cr - dr)}")
        print("  (a difference is normal when the company keeps closing stock outside the ledgers)")
        start = time.monotonic()
        recent_from = (to or date.today()) - timedelta(days=120)
        last = t.last_voucher_date(name, recent_from, to or date.today())
        print(f"Last entry (last 120 days): {last:%d %b %Y}" if last else "Last entry: none in the last 120 days",
              f"(read in {time.monotonic() - start:.1f}s)")
        if args.show:
            print("\nLargest balances:")
            for l in sorted(ledgers, key=lambda l: -abs(l.closing))[:15]:
                print(f"  {l.name[:40]:<40} {l.parent[:25]:<25} {dr_cr(l.closing):>20}")
    except TallyError as exc:
        print(f"FAILED: {exc}")
        return 1
    return 0


def _span(dates: list) -> str:
    return f"{len(dates):,} vouchers, {dates[0]:%d %b %Y} to {dates[-1]:%d %b %Y}" if dates else "no vouchers"


def _totals(rows) -> str:
    dr = sum(-v for v in rows if v < 0)
    cr = sum(v for v in rows if v > 0)
    return f"debits {dr:,.2f} / credits {cr:,.2f}"


def _date_check(t: Tally, name: str, frm: date, to: date) -> int:
    """Does Tally use the period in ACE's requests, or its own selected period?"""
    print(f"Period check, asking for {frm:%d %b %Y} to {to:%d %b %Y}:")
    tests = [("Vouchers, no period given", lambda: _span(t.voucher_dates(name))),
             ("Vouchers, period as variables", lambda: _span(t.voucher_dates(name, frm, to, strict=False))),
             ("Vouchers, period as filter", lambda: _span(t.voucher_dates(name, frm, to))),
             ("Ledger balances, no period", lambda: _totals([l.closing for l in t.ledgers(name)])),
             (f"Ledger balances to {to:%d %b %Y}", lambda: _totals([l.closing for l in t.ledgers(name, frm, to)])),
             ("Trial Balance, no period", lambda: _totals([v for _, v in t.trial_balance(name)])),
             (f"Trial Balance to {to:%d %b %Y}", lambda: _totals([v for _, v in t.trial_balance(name, frm, to)]))]
    for label, fn in tests:
        start = time.monotonic()
        try:
            out = fn()
        except TallyError as exc:
            out = f"FAILED: {exc}"
        print(f"  {label}: {out} ({time.monotonic() - start:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
