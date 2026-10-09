"""Tally check types (v0.8.0) - the building blocks of Tally tasks.

Each check looks at ONE company and returns a Result: ok, fail (a finding), info (a report or an
answer) or error. Tasks combine a check with settings, companies and a schedule (app/tally_tasks.py).
All reads go through the read-only Tally client.

Ledger targets (params "ledger" / "group"):
  ledger: a ledger name (exact, else partial), or a role from the client register:
          "suspense", "bank", "cash" (ACE finds them itself if the register has none)
  group:  a Tally group, sub-groups included (e.g. "Sundry Debtors", "Duties & Taxes")
Periods ("period"): today, yesterday, last_N_days (e.g. last_7_days), this_week, last_week,
  this_month, last_month, this_quarter, last_quarter, this_year (financial), last_year,
  or "2026-01-01..2026-03-31".
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from integrations.tally.client import dr_cr, in_group


@dataclass
class Result:
    status: str                      # ok | fail | info | error
    summary: str
    details: list[str] = field(default_factory=list)
    file: str | None = None
    rows: list[dict] = field(default_factory=list)    # every item, for the Excel list of findings


class CheckError(ValueError):
    pass


# --- context: one run, cached reads ------------------------------------------------------
class Ctx:
    def __init__(self, tally, register: dict | None = None, today: date | None = None, store=None,
                 ask: Callable[[str, str], str] | None = None) -> None:
        self.tally = tally
        self.register = register or {}
        if today is None:
            from app import tasks
            today = tasks.now().date()                    # ACE's clock (Asia/Dubai)
        self.today = today
        self.store = store
        self.ask = ask
        self._cache: dict = {}
        self._companies = None

    def companies(self) -> list[dict]:
        if self._companies is None:
            self._companies = self.tally.companies()
        return self._companies

    def _get(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def groups(self, company):
        return self._get(("groups", company), lambda: self.tally.groups(company))

    def ledgers(self, company, frm=None, to=None):
        return self._get(("ledgers", company, frm, to), lambda: self.tally.ledgers(company, frm, to))

    def label(self, company: str) -> str:
        c = self.register.get(company)
        return c.label if c else company

    def fy_start(self, company: str, d: date) -> date:
        """Start of the financial year that contains d."""
        month = 1
        c = self.register.get(company)
        m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", (c.fy_start if c else "").lower())
        if m:
            month = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(m.group(1)) + 1
        else:
            info = next((x for x in self.companies() if x["name"] == company), None)
            if info and info.get("starting_from"):
                month = info["starting_from"].month
        y = d.year if d.month >= month else d.year - 1
        return date(y, month, 1)

    def roles(self, company: str) -> dict[str, list[str]]:
        c = self.register.get(company)
        if c and any(c.roles.values()):
            return c.roles
        from app.tally_register import find_roles
        return self._get(("roles", company), lambda: find_roles(self.ledgers(company), self.groups(company)))

    def target(self, company: str, p: dict, frm=None, to=None) -> tuple[str, list]:
        """(description, ledgers) for the params' ledger / group."""
        ledgers = self.ledgers(company, frm, to)
        if p.get("group"):
            g = p["group"]
            groups = self.groups(company)
            if not any(n.lower() == g.lower() for n in groups):
                raise CheckError(f"no group '{g}' in this company")
            return f"group {g}", [l for l in ledgers if in_group(l.parent, g, groups)]
        want = (p.get("ledger") or "").strip()
        if not want:
            raise CheckError("the check needs a ledger or a group")
        role = want.lower().strip("@ ")
        if role in ("suspense", "bank", "cash"):
            names = {n.lower() for n in self.roles(company).get(role, [])}
            found = [l for l in ledgers if l.name.lower() in names]
            if not found:
                raise CheckError(f"no {role} ledger found (fill 'Suspense/Bank/Cash ledgers' in the register)")
            return f"{role} ledger{'s' if len(found) > 1 else ''}", found
        exact = [l for l in ledgers if l.name.lower() == want.lower()]
        found = exact or [l for l in ledgers if want.lower() in l.name.lower()]
        if not found:
            raise CheckError(f"no ledger '{want}' in this company")
        return (found[0].name if len(found) == 1 else f"ledgers like '{want}'"), found


# --- periods -------------------------------------------------------------------------------
def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def _month_end(d: date) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def period(spec: str | None, today: date, fy_start: date | None = None) -> tuple[date, date]:
    s = (spec or "last_7_days").strip().lower().replace(" ", "_")
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", s)
    if m:
        a, b = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        return min(a, b), max(a, b)
    m = re.fullmatch(r"last_(\d+)_days?", s)
    if m:
        return today - timedelta(days=int(m.group(1)) - 1), today
    if s == "today":
        return today, today
    if s == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if s == "this_week":
        return today - timedelta(days=today.weekday()), today
    if s == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    if s == "this_month":
        return today.replace(day=1), today
    if s == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if s == "this_quarter":
        return _quarter_start(today), today
    if s == "last_quarter":
        end = _quarter_start(today) - timedelta(days=1)
        return _quarter_start(end), end
    fy = fy_start or date(today.year, 1, 1)
    if s in ("this_year", "ytd"):
        return fy, today
    if s == "last_year":
        start = fy.replace(year=fy.year - 1)
        return start, fy - timedelta(days=1)
    raise CheckError(f"I don't know the period '{spec}'")


def _previous(frm: date, to: date, how: str) -> tuple[date, date]:
    if how == "last_year":
        return frm.replace(year=frm.year - 1), to.replace(year=to.year - 1)
    if frm.day == 1 and to == _month_end(to) and (to.year * 12 + to.month) - (frm.year * 12 + frm.month) in (0, 2, 11):
        months = (to.year * 12 + to.month) - (frm.year * 12 + frm.month) + 1
        start_m = frm.year * 12 + frm.month - 1 - months
        pfrm = date(start_m // 12, start_m % 12 + 1, 1)
        return pfrm, frm - timedelta(days=1)
    days = (to - frm).days + 1
    return frm - timedelta(days=days), frm - timedelta(days=1)


def _money(v: float) -> str:
    return dr_cr(v)


def _num(p: dict, key: str, default: float) -> float:
    try:
        return float(str(p.get(key, default)).replace(",", ""))
    except ValueError as exc:
        raise CheckError(f"'{key}' must be a number") from exc


# --- the checks ----------------------------------------------------------------------------
def _vamount(v: dict) -> float:
    """Voucher amount: total of the debit lines."""
    return sum(-a for _, a in v["lines"] if a < 0)


def _kind(v: dict) -> int:
    """Sort order for future entries: likely errors first, then post-dated cheques, then journals."""
    t = v["type"].lower()
    if "journal" in t:
        return 2
    if "chq" in t or "cheque" in t or "pdc" in t:
        return 1
    return 0


def _vline(v: dict, extra: str = "") -> str:
    parts = [f"{v['date']:%d %b %Y} {v['type']} {v['number']}".strip()]
    if v.get("party"):
        parts.append(v["party"])
    amt = _vamount(v)
    if amt:
        parts.append(f"{amt:,.2f}")
    if extra:
        parts.append(extra)
    if v.get("narration"):
        parts.append(" ".join(v["narration"].split())[:90])
    return " - ".join(parts)


def _vrow(v: dict, **more) -> dict:
    return {"Date": v["date"], "Type": v["type"], "Number": v["number"], "Party": v.get("party", ""),
            "Amount": _vamount(v), **more, "Narration": " ".join((v.get("narration") or "").split())}


def future_entries(ctx: Ctx, company: str, p: dict) -> Result:
    grace = int(_num(p, "grace_days", 0))
    start = ctx.today + timedelta(days=grace + 1)
    vs = ctx.tally.vouchers(company, start, ctx.today + timedelta(days=3650))
    if not vs:
        return Result("ok", "no entries dated in the future")
    vs.sort(key=lambda v: (_kind(v), v["date"]))
    types: dict[str, int] = {}
    for v in vs:
        types[v["type"] or "?"] = types.get(v["type"] or "?", 0) + 1
    by_type = ", ".join(f"{t} {n}" for t, n in sorted(types.items(), key=lambda x: -x[1]))
    latest = max(v["date"] for v in vs)
    return Result("fail", f"{len(vs)} entr{'y' if len(vs) == 1 else 'ies'} dated in the future "
                          f"(latest {latest:%d %b %Y}): {by_type}",
                  [_vline(v) for v in vs], rows=[_vrow(v) for v in vs])


VTYPES = {"sales": "IsSales", "purchase": "IsPurchase", "receipt": "IsReceipt", "payment": "IsPayment",
          "journal": "IsJournal", "contra": "IsContra", "credit_note": "IsCreditNote", "debit_note": "IsDebitNote"}


def last_entry(ctx: Ctx, company: str, p: dict) -> Result:
    max_days = int(_num(p, "max_days", 7))
    vtype = (p.get("voucher_type") or "").strip().lower().replace(" ", "_")
    window = max(400, max_days * 3)
    extra = {}
    if vtype and vtype != "any":
        fn = VTYPES.get(vtype)
        extra["ACEType"] = f'$${fn}:$VoucherTypeName' if fn else f'$VoucherTypeName = "{p["voucher_type"]}"'
    dates = ctx.tally.voucher_dates(company, ctx.today - timedelta(days=window), ctx.today, extra=extra)
    what = f"{vtype.replace('_', ' ')} entry" if extra else "entry"
    if not dates:
        return Result("fail", f"no {what} in the last {window} days")
    last = dates[-1]
    age = (ctx.today - last).days
    if age > max_days:
        return Result("fail", f"last {what} {last:%d %b %Y} ({age} days ago, limit {max_days})")
    return Result("ok", f"last {what} {last:%d %b %Y}")


def ledger_balance(ctx: Ctx, company: str, p: dict) -> Result:
    as_of = date.fromisoformat(p["as_of"]) if p.get("as_of") else ctx.today
    rule = (p.get("rule") or "zero").strip().lower()
    limit = _num(p, "amount", 0)
    desc, ledgers = ctx.target(company, p, ctx.fy_start(company, as_of), as_of)
    bad = []
    for l in ledgers:
        v = l.closing
        wrong = {"zero": abs(v) >= 0.01, "no_credit": v > 0.005, "no_debit": v < -0.005,
                 "max": abs(v) > limit, "min": abs(v) < limit}.get(rule)
        if wrong is None:
            raise CheckError(f"unknown rule '{rule}' (zero, no_credit, no_debit, max, min)")
        if wrong:
            bad.append(f"{l.name}: {_money(v)}")
    rules = {"zero": "should be zero", "no_credit": "should not be in credit", "no_debit": "should not be in debit",
             "max": f"should not exceed {limit:,.2f}", "min": f"should be at least {limit:,.2f}"}
    if bad:
        return Result("fail", f"{desc} {rules[rule]} (as at {as_of:%d %b %Y})", bad[:15])
    return Result("ok", f"{desc} fine as at {as_of:%d %b %Y}")


def ledger_postings(ctx: Ctx, company: str, p: dict) -> Result:
    frm, to = period(p.get("period"), ctx.today, ctx.fy_start(company, ctx.today))
    min_amount = _num(p, "min_amount", 0)
    desc, ledgers = ctx.target(company, p)
    names = {l.name.lower() for l in ledgers}
    hits = []
    for v in ctx.tally.vouchers(company, frm, to):
        for ledger, amt in v["lines"]:
            if ledger.lower() in names and abs(amt) >= min_amount:
                hits.append((v, ledger, amt))
    if not hits:
        return Result("ok", f"no postings to {desc} {frm:%d %b} - {to:%d %b %Y}")
    hits.sort(key=lambda h: h[0]["date"] or date.min)
    details = [_vline(v, f"{ledger}: {_money(a)}") for v, ledger, a in hits]
    total = sum(a for _, _, a in hits)
    return Result("fail", f"{len(hits)} posting{'s' if len(hits) > 1 else ''} to {desc} "
                          f"{frm:%d %b} - {to:%d %b %Y} (net {_money(total)})", details,
                  rows=[_vrow(v, Ledger=ledger, **{"Ledger amount": a}) for v, ledger, a in hits])


def _movement(ctx: Ctx, company: str, p: dict, frm: date, to: date) -> tuple[str, float]:
    desc, ledgers = ctx.target(company, p, frm, to)
    return desc, sum(l.closing - l.opening for l in ledgers)


def compare(ctx: Ctx, company: str, p: dict) -> Result:
    frm, to = period(p.get("period") or "last_month", ctx.today, ctx.fy_start(company, ctx.today))
    pfrm, pto = _previous(frm, to, (p.get("versus") or "previous").lower())
    threshold = _num(p, "threshold_pct", 30)
    min_amount = _num(p, "min_amount", 0)
    desc, now_v = _movement(ctx, company, p, frm, to)
    _, prev_v = _movement(ctx, company, p, pfrm, pto)
    line = (f"{desc}: {_money(now_v)} ({frm:%d %b} - {to:%d %b %Y}) vs {_money(prev_v)} "
            f"({pfrm:%d %b} - {pto:%d %b %Y})")
    if max(abs(now_v), abs(prev_v)) < max(min_amount, 0.01):
        return Result("ok", line + " - too small to compare")
    if abs(prev_v) < 0.01:
        return Result("fail", line + " - nothing in the earlier period")
    change = (abs(now_v) - abs(prev_v)) / abs(prev_v) * 100
    status = "fail" if abs(change) > threshold else "ok"
    return Result(status, f"{line}: {change:+.0f}%")


def report(ctx: Ctx, company: str, p: dict) -> Result:
    """Trial Balance or ledger balances as a branded Excel file (Accountability house style)."""
    from openpyxl import Workbook

    from app.brand import AED, Sheet, save
    from app.storage import free_name
    kind = (p.get("kind") or "trial_balance").lower()
    as_of = date.fromisoformat(p["as_of"]) if p.get("as_of") else ctx.today
    frm = ctx.fy_start(company, as_of)
    client = ctx.label(company)
    sub = (f"{company} | Financial year from {frm:%d-%b-%Y} | as at {as_of:%d-%b-%Y} | AED | Source: Tally (read-only) | "
           f"Prepared by ACE (Accountability's Chief Examiner) | DRAFT FOR REVIEW")
    wb = Workbook()
    if kind == "trial_balance":
        sh = Sheet(wb, "Trial Balance", f"Trial Balance - {client} - {as_of:%d %b %Y}", sub, [50, 20, 20], first=True)
        sh.header(["Particulars", "Debit", "Credit"])
        dr = cr = 0.0
        for name, v in ctx.tally.trial_balance(company, frm, as_of):
            sh.line([name, -v if v < 0 else None, v if v > 0 else None], [None, AED, AED])
            dr += -v if v < 0 else 0
            cr += v if v > 0 else 0
        sh.line(["Total", dr, cr], [None, AED, AED], "total")
        if abs(dr - cr) >= 0.01:
            sh.note(f"Difference {dr - cr:,.2f} - check opening balances / closing stock in Tally.")
        engagement = "Trial Balance"
    elif kind == "ledger_balances":
        sh = Sheet(wb, "Ledger balances", f"Ledger Balances - {client} - {as_of:%d %b %Y}", sub, [45, 30, 20, 20],
                   first=True)
        sh.header(["Ledger", "Group", "Debit", "Credit"])
        rows = [l for l in ctx.ledgers(company, frm, as_of) if abs(l.closing) >= 0.005]
        dr = cr = 0.0
        for group in sorted({l.parent for l in rows}, key=str.lower):
            sh.band(group or "(no group)")
            gdr = gcr = 0.0
            for l in sorted((l for l in rows if l.parent == group), key=lambda l: l.name.lower()):
                d, c = (-l.closing, None) if l.closing < 0 else (None, l.closing)
                sh.line([l.name, group, d, c], [None, None, AED, AED])
                gdr += d or 0
                gcr += c or 0
            sh.line([f"Total {group}", "", gdr or None, gcr or None], [None, None, AED, AED], "subtotal")
            dr, cr = dr + gdr, cr + gcr
        sh.line(["Total", "", dr, cr], [None, None, AED, AED], "total")
        sh.note("Ledger closing balances from Tally; closing stock is not a ledger and is not included.")
        engagement = "Ledger Balances"
    else:
        raise CheckError(f"unknown report '{kind}' (trial_balance, ledger_balances)")
    sh.footer(client, engagement)
    title = re.sub(r'[\\/:*?"<>|]', "-", f"{client} - {engagement} {as_of:%Y-%m-%d}.xlsx")
    folder = ["reports", re.sub(r'[\\/:*?"<>|]', "-", client)]
    store = ctx.store
    if store is None:
        from app.tally_register import _store
        store = _store()
    name = free_name(store, folder, title)
    where = store.save(folder + [name], save(wb))
    return Result("info", f"{engagement.lower()} as at {as_of:%d %b %Y} saved", file=where)


def ask(ctx: Ctx, company: str, p: dict) -> Result:
    q = (p.get("question") or "").strip()
    if not q:
        raise CheckError("the check needs a question")
    if not ctx.ask:
        raise CheckError("the AI is not available")
    return Result("info", ctx.ask(company, q))


@dataclass
class CheckType:
    fn: Callable[[Ctx, str, dict], Result]
    description: str
    params: str


CHECKS: dict[str, CheckType] = {
    "future_entries": CheckType(future_entries, "Entries dated in the future (typing mistakes in dates).",
                                "grace_days (default 0)"),
    "last_entry": CheckType(last_entry, "Books not updated: last entry older than max_days.",
                            "max_days (default 7); voucher_type: any|sales|purchase|receipt|payment|journal|contra or a "
                            "voucher type name"),
    "ledger_balance": CheckType(ledger_balance, "A ledger / group balance breaks a rule (e.g. suspense must be zero, "
                                "cash must not be in credit).",
                                "ledger or group; rule: zero|no_credit|no_debit|max|min; amount (for max/min); as_of "
                                "(YYYY-MM-DD, default today)"),
    "ledger_postings": CheckType(ledger_postings, "Entries posted to a ledger / group in a period (e.g. anything to "
                                 "suspense this week, entries above an amount).",
                                 "ledger or group; period (default last_7_days); min_amount"),
    "compare": CheckType(compare, "A ledger / group moved more than threshold_pct against the previous period or the "
                         "same period last year.",
                         "ledger or group; period (default last_month); versus: previous|last_year; threshold_pct "
                         "(default 30); min_amount"),
    "report": CheckType(report, "Save a Trial Balance or ledger balances to Excel in ACE/tally/reports/<client>/.",
                        "kind: trial_balance|ledger_balances; as_of (default today)"),
    "ask": CheckType(ask, "A question in your own words, answered by the AI from Tally data (costs more).",
                     "question"),
}


def run_check(ctx: Ctx, check: str, company: str, params: dict) -> Result:
    ct = CHECKS.get(check)
    if not ct:
        return Result("error", f"unknown check '{check}'")
    try:
        return ct.fn(ctx, company, params or {})
    except CheckError as exc:
        return Result("error", str(exc))
    except Exception as exc:  # noqa: BLE001 - one company must not stop the others
        return Result("error", f"{type(exc).__name__}: {exc}")


def types_text() -> str:
    out = ["Tally check types:"]
    for name, ct in CHECKS.items():
        out.append(f"- {name}: {ct.description} Settings: {ct.params}")
    return "\n".join(out)
