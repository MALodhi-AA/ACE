"""Tally tasks (v0.8.0): checks Sir Muhammad Ali sets up in plain words.

A task = a check type (app/tally_checks.py) + settings + which companies + a schedule +
how to report. ACE runs it on schedule (or on "tally run <n>") and sends the findings to
Sir Muhammad Ali only. Every run is kept (table tally_runs) so trends can be asked about.

Schedules: manual | daily HH:MM | weekdays HH:MM | weekly <day> HH:MM | monthly <day> HH:MM
Companies: "all" (register: Include = Y) | list of names / short names | {"staff": name} |
           {"manager": name} | {"vat": "monthly" / "quarterly"}
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date, datetime, time, timedelta

from app import tasks as _tasks
from app.tasks import tz


def now():
    return _tasks.now()

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tally_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  check_type TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{}',
  companies TEXT NOT NULL DEFAULT '"all"',
  schedule TEXT NOT NULL DEFAULT 'manual',
  report TEXT NOT NULL DEFAULT 'exceptions',     -- exceptions | always
  enabled INTEGER NOT NULL DEFAULT 1,
  next_run TEXT, last_run TEXT, last_summary TEXT,
  created_by TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tally_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER, ts TEXT, company TEXT, status TEXT, summary TEXT, details TEXT
);
"""

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_run_lock = threading.Lock()


# --- schedules -----------------------------------------------------------------------------
def parse_schedule(s: str) -> str:
    """Normalised schedule text, or ValueError."""
    t = (s or "manual").strip().lower()
    if t in ("manual", "on demand", "none", ""):
        return "manual"
    m = re.fullmatch(r"(daily|weekdays)\s+(\d{1,2}):(\d{2})", t)
    if m:
        return f"{m.group(1)} {int(m.group(2)):02d}:{m.group(3)}"
    m = re.fullmatch(r"weekly\s+([a-z]{3})[a-z]*\s+(\d{1,2}):(\d{2})", t)
    if m and m.group(1) in DAYS:
        return f"weekly {m.group(1)} {int(m.group(2)):02d}:{m.group(3)}"
    m = re.fullmatch(r"monthly\s+(\d{1,2})\s+(\d{1,2}):(\d{2})", t)
    if m and 1 <= int(m.group(1)) <= 28:
        return f"monthly {int(m.group(1))} {int(m.group(2)):02d}:{m.group(3)}"
    raise ValueError(f"I can't read the schedule '{s}' (manual, daily 10:00, weekdays 10:00, "
                     "weekly mon 10:00, monthly 5 10:00 - day 1 to 28)")


def next_run(schedule: str, after: datetime) -> datetime | None:
    s = parse_schedule(schedule)
    if s == "manual":
        return None
    parts = s.split()
    hh, mm = map(int, parts[-1].split(":"))
    at = time(hh, mm)
    d = after.date()
    for _ in range(400):
        cand = datetime.combine(d, at, tz())
        ok = cand > after and (
            parts[0] == "daily"
            or (parts[0] == "weekdays" and d.weekday() < 5)
            or (parts[0] == "weekly" and d.weekday() == DAYS.index(parts[1]))
            or (parts[0] == "monthly" and d.day == int(parts[1])))
        if ok:
            return cand
        d += timedelta(days=1)
    return None


def schedule_text(s: str) -> str:
    s = parse_schedule(s)
    if s == "manual":
        return "only when asked"
    p = s.split()
    if p[0] == "daily":
        return f"every day at {p[1]}"
    if p[0] == "weekdays":
        return f"every working day (Mon-Fri) at {p[1]}"
    if p[0] == "weekly":
        return f"every {p[1].title()} at {p[2]}"
    return f"on the {p[1]}{'st' if p[1] in ('1', '21') else 'nd' if p[1] in ('2', '22') else 'rd' if p[1] in ('3', '23') else 'th'} of each month at {p[2]}"


def companies_text(spec) -> str:
    if spec in (None, "all", ["all"]):
        return "all companies"
    if isinstance(spec, dict):
        k, v = next(iter(spec.items()))
        return {"staff": f"companies kept by {v}", "manager": f"companies managed by {v}",
                "vat": f"{v} VAT companies", "group": f"all units of the {v} group",
                "entity": f"all units of entity {v}"}.get(k, f"{k} = {v}")
    return ", ".join(spec)


# --- store -------------------------------------------------------------------------------------
class TallyTasks:
    def __init__(self, store) -> None:
        self.store = store                      # app.tasks.TaskStore (shares its SQLite file)
        with store._lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    def _q(self, sql, args=()):
        with self.store._lock:
            cur = self.store.db.execute(sql, args)
            self.store.db.commit()
            return cur

    @staticmethod
    def _row(r) -> dict | None:
        if not r:
            return None
        d = dict(r)
        d["params"] = json.loads(d["params"] or "{}")
        d["companies"] = json.loads(d["companies"] or '"all"')
        return d

    def add(self, task: dict, by: str = "") -> dict:
        sched = parse_schedule(task.get("schedule"))
        nr = next_run(sched, now())
        cur = self._q("INSERT INTO tally_tasks (name, check_type, params, companies, schedule, report, next_run, "
                      "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (task["name"], task["check"], json.dumps(task.get("params") or {}),
                       json.dumps(task.get("companies") or "all"), sched, task.get("report") or "exceptions",
                       nr.isoformat(timespec="seconds") if nr else None, by, now().isoformat(timespec="seconds")))
        return self.get(cur.lastrowid)

    def get(self, tid: int) -> dict | None:
        return self._row(self._q("SELECT * FROM tally_tasks WHERE id=?", (tid,)).fetchone())

    def all(self) -> list[dict]:
        return [self._row(r) for r in self._q("SELECT * FROM tally_tasks ORDER BY id").fetchall()]

    def update(self, tid: int, **fields) -> None:
        if fields:
            self._q(f"UPDATE tally_tasks SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), tid))

    def delete(self, tid: int) -> bool:
        return self._q("DELETE FROM tally_tasks WHERE id=?", (tid,)).rowcount > 0

    def due(self, at: datetime) -> list[dict]:
        return [t for t in self.all() if t["enabled"] and t["next_run"] and datetime.fromisoformat(t["next_run"]) <= at]

    def record(self, tid: int, company: str, res) -> None:
        self._q("INSERT INTO tally_runs (task_id, ts, company, status, summary, details) VALUES (?,?,?,?,?,?)",
                (tid, now().isoformat(timespec="seconds"), company, res.status, res.summary, json.dumps(res.details)))

    def runs(self, tid: int, limit: int = 200) -> list[dict]:
        rows = self._q("SELECT * FROM tally_runs WHERE task_id=? ORDER BY id DESC LIMIT ?", (tid, limit)).fetchall()
        return [dict(r) for r in rows]


# --- running -----------------------------------------------------------------------------------
def select_companies(spec, open_companies: list[str], register: dict) -> tuple[list[str], list[str]]:
    """(companies to check, names that matched nothing)."""
    from app.tally_register import match_company
    if spec in (None, "all", ["all"]):
        # closed companies ("Z (Closed) ...") are left out unless the register says Include = Y
        return [c for c in open_companies
                if (register[c].include if register.get(c) else not re.search(r"\(closed\)", c, re.I))], []
    if isinstance(spec, dict):
        k, v = next(iter(spec.items()))
        v = str(v).strip().lower()
        pick = []
        for c in open_companies:
            r = register.get(c)
            if not r or not r.include:
                continue
            field = {"staff": r.staff, "manager": r.manager, "vat": r.vat_period, "group": r.group,
                     "entity": r.entity}.get(k, "")
            if k == "entity":
                from app.tally_register import entity_code
                hit = bool(field) and field.lower() == (entity_code(v) or v).lower()
            elif k == "group":
                hit = bool(field) and field.lower() == v
            else:
                hit = bool(v) and v in field.lower()
            if hit:
                pick.append(c)
        return pick, ([] if pick else [f"{k} = {v} (fill the client register)"])
    out, missing = [], []
    for name in spec if isinstance(spec, list) else [spec]:
        found = match_company(str(name), open_companies, register)
        if found:
            out += [f for f in found if f not in out]
        else:
            missing.append(str(name))
    return out, missing


def run_task(task: dict, tally, tasks: TallyTasks, register: dict | None = None, only: str | None = None,
             today: date | None = None, ask=None, store=None) -> str:
    """Run one task over its companies; record the results; return the message for Sir Muhammad Ali."""
    from app.tally_checks import Ctx, run_check
    from app.tally_register import load, match_company
    with _run_lock:                                     # one Tally job at a time
        register = register if register is not None else load()
        ctx = Ctx(tally, register, today=today, store=store, ask=ask)
        names = [c["name"] for c in ctx.companies()]
        if only:
            companies = match_company(only, names, register)
            missing = [] if companies else [only]
        else:
            companies, missing = select_companies(task["companies"], names, register)
        results = []
        for c in companies:
            res = run_check(ctx, task["check_type"], c, task["params"])
            tasks.record(task["id"], c, res)
            results.append((c, res))
        fails = [(c, r) for c, r in results if r.status == "fail"]
        errors = [(c, r) for c, r in results if r.status == "error"]
        infos = [(c, r) for c, r in results if r.status == "info"]
        oks = [(c, r) for c, r in results if r.status == "ok"]
        head = (f"Tally check {task['id']} '{task['name']}' - {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}: "
                + ", ".join(x for x in [f"{len(oks)} clear" if oks else "",
                                        f"{len(fails)} need attention" if fails else "",
                                        f"{len(infos)} done" if infos else "",
                                        f"{len(errors)} could not be checked" if errors else ""] if x))
        lines = [head]
        listing = _findings_file(task, [(ctx.label(c), r) for c, r in fails + oks if r.rows], store, ctx.today)
        for c, r in fails + infos:
            lines.append(f"- {ctx.label(c)}: {r.summary}" + (f" -> {r.file}" if r.file else ""))
            items = [d for d in r.details if not d.endswith("not counted")]
            notes = [d for d in r.details if d.endswith("not counted")]
            lines += [f"    {d}" for d in items[:SHOW]]
            if len(items) > SHOW:
                lines.append(f"    ... and {len(items) - SHOW} more" + (" (all in the Excel list)" if listing else ""))
            lines += [f"    {d}" for d in notes]
        if listing:
            lines.append(f"Full list with narrations (every entry, grouped): {listing}")
        if task.get("report") == "always" and oks:
            lines.append("Clear: " + ", ".join(ctx.label(c) for c, _ in oks))
        by_reason: dict[str, list[str]] = {}
        for c, r in errors:
            by_reason.setdefault(r.summary, []).append(ctx.label(c))
        for reason, names_ in by_reason.items():
            if len(names_) <= 3:
                lines += [f"- {n}: could not check - {reason}" for n in names_]
            else:                                         # one line for the same problem in many companies
                lines.append(f"- {len(names_)} companies could not be checked - {reason}: "
                             + ", ".join(names_[:5]) + (" ..." if len(names_) > 5 else ""))
        if missing:
            lines.append("Not found in Tally (is it open?): " + ", ".join(missing))
        if register:
            from app.tally_register import check
            issues = check(register, open_companies=names)
            if issues:
                lines.append(f"Client register: {len(issues)} point{'s' if len(issues) > 1 else ''} to fix - "
                             "send 'check client register'.")
        if not companies:
            lines.append("No companies to check.")
        summary = head.split(": ", 1)[-1]
        tasks.update(task["id"], last_run=now().isoformat(timespec="seconds"), last_summary=summary)
        return "\n".join(lines)


SHOW = 8                                               # items per company in the chat message


def _findings_file(task: dict, results: list, store, today) -> str | None:
    """All findings of a run as one branded Excel list (when there are more than fit in chat)."""
    shown = lambda r: min(len([d for d in r.details if not d.endswith("not counted")]), SHOW) if r.status == "fail" else 0  # noqa: E731
    if not results or all(len(r.rows) <= shown(r) for _, r in results):
        return None
    results = sorted(results, key=lambda x: (x[1].status != "fail", x[0].lower()))
    import re as _re

    from openpyxl import Workbook

    from app.brand import AED, DATE, Sheet, save
    from app.storage import free_name
    keys: list[str] = []
    for _, r in results:
        for row in r.rows:
            keys += [k for k in row if k not in keys]
    wb = Workbook()
    widths = {"Date": 12, "Type": 18, "Number": 16, "Party": 30, "Amount": 15, "Ledger": 26, "Ledger amount": 15,
              "Narration": 70}
    sh = Sheet(wb, "Findings", f"Tally check {task['id']} - {task['name']}",
               f"Run {today:%d-%b-%Y} | {sum(len(r.rows) for _, r in results)} items in {len(results)} companies | "
               "Source: Tally (read-only) | Prepared by ACE", [32] + [widths.get(k, 18) for k in keys], first=True)
    sh.header(["Company"] + keys)
    for label, r in results:
        sh.band(f"{label} - {r.summary}")
        for row in r.rows:
            sh.line([label] + [row.get(k) for k in keys],
                    [None] + [DATE if k == "Date" else AED if "mount" in k else None for k in keys])
    sh.footer("All clients", f"Tally check {task['id']}")
    if store is None:
        from app.tally_register import _store
        store = _store()
    name = _re.sub(r'[\\/:*?"<>|]', "-", f"Check {task['id']} {task['name']} {today:%Y-%m-%d}.xlsx")
    name = free_name(store, ["findings"], name)
    try:
        return store.save(["findings", name], save(wb))
    except Exception as exc:  # noqa: BLE001 - the chat message still goes out
        log.warning("findings file: %s", exc)
        return None


def task_text(t: dict) -> str:
    p = ", ".join(f"{k}={v}" for k, v in (t["params"] or {}).items()) or "default settings"
    state = "" if t["enabled"] else " (paused)"
    nr = f"; next {datetime.fromisoformat(t['next_run']):%a %d %b %H:%M}" if t.get("next_run") and t["enabled"] else ""
    last = f"; last run {datetime.fromisoformat(t['last_run']):%d %b %H:%M}: {t['last_summary']}" if t.get("last_run") else ""
    return (f"{t['id']}. {t['name']}{state} - {t['check_type']} ({p}); {companies_text(t['companies'])}; "
            f"{schedule_text(t['schedule'])}; report {'everything' if t['report'] == 'always' else 'only problems'}{nr}{last}")


def list_text(tasks: TallyTasks) -> str:
    rows = tasks.all()
    if not rows:
        return ("No Tally tasks yet. Tell me what to check, e.g. 'tally task: every Monday at 10 check all "
                "companies - suspense must be zero'. 'tally check types' lists what I can check.")
    return "Tally tasks:\n" + "\n".join(task_text(t) for t in rows) + \
        "\n(tally run <n> [for <company>] / tally pause <n> / tally resume <n> / tally delete <n>)"


# --- creating a task from plain words ------------------------------------------------------------
PARAM_KEYS = {"ledger", "group", "rule", "amount", "as_of", "period", "min_amount", "versus", "threshold_pct",
              "max_days", "voucher_type", "grace_days", "kind", "question", "include_pdc", "include_journals"}


def draft_prompt(today: date) -> str:
    from app.tally_checks import CHECKS
    checks = "\n".join(f"- {n}: {c.description} Settings: {c.params}" for n, c in CHECKS.items())
    return (f"Turn Sir Muhammad Ali's instruction into ONE Tally check task for his assistant ACE. Today is "
            f"{today:%A %d %B %Y}.\nCheck types:\n{checks}\n"
            "ledger may be a ledger name or one of: suspense, bank, cash. group is a Tally group "
            "(e.g. Sundry Debtors, Duties & Taxes, Sales Accounts).\n"
            "period: today | yesterday | last_N_days | this_week | last_week | this_month | last_month | this_quarter | "
            "last_quarter | this_year | last_year | YYYY-MM-DD..YYYY-MM-DD\n"
            "schedule: manual | daily HH:MM | weekdays HH:MM | weekly mon HH:MM | monthly D HH:MM (D 1-28), 24h clock.\n"
            "companies: \"all\" | [list of company names as written] | {\"staff\": name} | {\"manager\": name} | "
            "{\"vat\": \"monthly\"|\"quarterly\"} | {\"group\": name} | {\"entity\": \"E-1\"}\n"
            "report: exceptions (only problems, default) | always\n"
            "Reply with JSON only: {\"name\": short title, \"check\": type, \"params\": {...}, \"companies\": ..., "
            "\"schedule\": ..., \"report\": ..., \"question\": null or what is unclear}")


def validate(d: dict) -> list[str]:
    from app.tally_checks import CHECKS
    problems = []
    if d.get("check") not in CHECKS:
        problems.append(f"unknown check type '{d.get('check')}'")
    try:
        d["schedule"] = parse_schedule(d.get("schedule") or "manual")
    except ValueError as exc:
        problems.append(str(exc))
    params = d.get("params") or {}
    d["params"] = {k: v for k, v in params.items() if k in PARAM_KEYS and v not in (None, "")}
    if d.get("check") in ("ledger_balance", "ledger_postings", "compare") and not (d["params"].get("ledger") or d["params"].get("group")):
        problems.append("which ledger or group?")
    if d.get("check") == "ask" and not d["params"].get("question"):
        problems.append("what is the question?")
    if d.get("report") not in ("exceptions", "always"):
        d["report"] = "exceptions"
    if not d.get("companies"):
        d["companies"] = "all"
    if not d.get("name"):
        d["name"] = (d.get("check") or "Tally check").replace("_", " ")
    return problems


def _when(s: str) -> str:
    try:
        return schedule_text(s)
    except ValueError:
        return str(s)


def confirm_text(d: dict, problems: list[str]) -> str:
    p = ", ".join(f"{k}={v}" for k, v in d["params"].items()) or "default settings"
    lines = ["New Tally task:",
             f"Name: {d['name']}",
             f"Check: {d.get('check')} ({p})",
             f"Companies: {companies_text(d['companies'])}",
             f"When: {_when(d['schedule'])}",
             f"Report: {'everything' if d['report'] == 'always' else 'only problems'} - to you only"]
    if d.get("question"):
        lines.append(f"Unclear: {d['question']}")
    if problems:
        lines.append("Please fix: " + "; ".join(problems))
        lines.append("Tell me the correction, or NO to drop it.")
    else:
        lines.append("Reply YES to save it, tell me what to change, or NO to drop it.")
    return "\n".join(lines)
