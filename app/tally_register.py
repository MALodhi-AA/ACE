"""Client register for Tally (v0.8.0).

ACE drafts it from Tally ("draft client register"): one row per company open in Tally, with
what ACE can find itself (financial year start, bank / cash / suspense ledgers, last entry).
Sir Muhammad Ali's team fills in the rest (short name, responsible staff, manager, VAT period,
TRN) and saves it as ACE/tally/Client Register.xlsx. ACE reads that file before every Tally run.

The draft is always a new file (never overwrites).
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.config import settings

log = logging.getLogger(__name__)

FOLDER = ["tally"]
REGISTER = "Client Register.xlsx"
COLUMNS = ["Tally company", "Short name", "Include (Y/N)", "Responsible staff", "Manager", "VAT period",
           "VAT quarter ends", "FY start", "TRN", "Bank ledgers", "Cash ledgers", "Suspense ledgers",
           "Last entry (when drafted)", "Notes"]
HELP = [
    ("Tally company", "Exact company name in Tally - do not change."),
    ("Short name", "What you call the client in chat (e.g. Mara Al Wasl). ACE accepts it in instructions."),
    ("Include (Y/N)", "N = ACE leaves this company out of 'all companies' checks."),
    ("Responsible staff", "Who keeps the books (Chat name)."),
    ("Manager", "Who answers for the client (Chat name) - used when you ask ACE to take a finding up."),
    ("VAT period", "Monthly / Quarterly / None."),
    ("VAT quarter ends", "For quarterly: the months the quarters end, e.g. Mar Jun Sep Dec or Feb May Aug Nov."),
    ("FY start", "Financial year start (from Tally). Format 1-Jan or 1-Apr."),
    ("TRN", "VAT registration number."),
    ("Bank / Cash / Suspense ledgers", "Found by ACE from Tally groups and names; separate several with ';'. "
     "Checks that say 'suspense', 'bank' or 'cash' use these."),
    ("Last entry (when drafted)", "Information only."),
]


@dataclass
class Client:
    company: str
    short: str = ""
    include: bool = True
    staff: str = ""
    manager: str = ""
    vat_period: str = ""
    vat_quarter_ends: str = ""
    fy_start: str = ""
    trn: str = ""
    roles: dict[str, list[str]] = field(default_factory=dict)      # bank / cash / suspense -> ledgers
    notes: str = ""

    @property
    def label(self) -> str:
        return self.short or self.company


def _store():
    from app.nas import nas
    from app.storage import LocalStore, NasStore
    if nas.configured:
        return NasStore(settings.ace_share, FOLDER)
    return LocalStore(settings.data_dir / "tally")


def _read(store, name: str) -> bytes | None:
    from app.storage import LocalStore
    try:
        if isinstance(store, LocalStore):
            p = store.root / name
            return p.read_bytes() if p.exists() else None
        from app.nas import nas
        if not store.exists([name]):
            return None
        return nas.read_bytes(store.share, store.base + [name])
    except Exception as exc:  # noqa: BLE001
        log.warning("client register: %s", exc)
        return None


def _split(v) -> list[str]:
    return [x.strip() for x in re.split(r"[;\n]", str(v or "")) if x.strip()]


def load(store=None) -> dict[str, Client]:
    """The filled-in register, by Tally company name ({} if there is none yet)."""
    store = store or _store()
    data = _read(store, REGISTER)
    if not data:
        return {}
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {}
    head = [str(h or "").strip().lower() for h in rows[0]]

    def col(r, name):
        try:
            v = r[head.index(name.lower())]
        except (ValueError, IndexError):
            return ""
        return "" if v is None else str(v).strip()

    out = {}
    for r in rows[1:]:
        company = col(r, "Tally company")
        if not company:
            continue
        out[company] = Client(
            company=company, short=col(r, "Short name"),
            include=col(r, "Include (Y/N)").upper() not in ("N", "NO"),
            staff=col(r, "Responsible staff"), manager=col(r, "Manager"),
            vat_period=col(r, "VAT period"), vat_quarter_ends=col(r, "VAT quarter ends"),
            fy_start=col(r, "FY start"), trn=col(r, "TRN"), notes=col(r, "Notes"),
            roles={"bank": _split(col(r, "Bank ledgers")), "cash": _split(col(r, "Cash ledgers")),
                   "suspense": _split(col(r, "Suspense ledgers"))})
    return out


def find_roles(ledgers, groups: dict[str, str]) -> dict[str, list[str]]:
    """Bank / cash / suspense ledgers of one company, from Tally's groups and ledger names."""
    from integrations.tally.client import in_group
    roles = {"bank": [], "cash": [], "suspense": []}
    for l in ledgers:
        if in_group(l.parent, "Bank Accounts", groups) or in_group(l.parent, "Bank OD A/c", groups):
            roles["bank"].append(l.name)
        elif in_group(l.parent, "Cash-in-Hand", groups):
            roles["cash"].append(l.name)
        if "suspense" in l.name.lower() or in_group(l.parent, "Suspense A/c", groups):
            roles["suspense"].append(l.name)
    return roles


def draft(tally, store=None, today: date | None = None, existing: dict[str, Client] | None = None) -> tuple[str, int]:
    """Build the draft register from Tally. Returns (where it was saved, number of companies)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    from app.storage import free_name
    store = store or _store()
    today = today or date.today()
    existing = existing if existing is not None else load(store)
    wb = Workbook()
    ws = wb.active
    ws.title = "Clients"
    ws.append(COLUMNS)
    companies = sorted(tally.companies(), key=lambda c: c["name"].lower())
    for c in companies:
        name = c["name"]
        old = existing.get(name)
        roles, last = {"bank": [], "cash": [], "suspense": []}, None
        try:
            roles = find_roles(tally.ledgers(name), tally.groups(name))
            last = tally.last_voucher_date(name, today - timedelta(days=730), today)
        except Exception as exc:  # noqa: BLE001
            log.warning("register draft %s: %s", name, exc)
        fy = c.get("starting_from")
        ws.append([name,
                   old.short if old else re.sub(r"\s*\((?:E-\d+|c)\)\s*", " ", name).strip(),
                   ("Y" if old.include else "N") if old else "Y",
                   old.staff if old else "", old.manager if old else "",
                   old.vat_period if old else "", old.vat_quarter_ends if old else "",
                   old.fy_start if old and old.fy_start else (f"{fy.day}-{fy:%b}" if fy else ""),
                   old.trn if old else "",
                   "; ".join(roles["bank"]), "; ".join(roles["cash"]), "; ".join(roles["suspense"]),
                   f"{last:%d %b %Y}" if last else "none in 2 years",
                   old.notes if old else ""])
    navy = PatternFill("solid", fgColor="002D49")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF", name="Arial")
        cell.fill = navy
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    widths = [38, 22, 9, 20, 20, 12, 18, 9, 18, 40, 30, 30, 16, 30]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    ws.freeze_panes = "B2"
    h = wb.create_sheet("How to fill")
    h.append(["Column", "What to put"])
    for row in HELP:
        h.append(list(row))
    h.append([])
    h.append(["When done", f"Save this sheet as '{REGISTER}' in the ACE/tally folder (replace the old one). "
              "ACE reads it before every Tally check."])
    h.column_dimensions["A"].width = 30
    h.column_dimensions["B"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    name = free_name(store, [], f"Client Register (draft) {today:%Y-%m-%d}.xlsx")
    where = store.save([name], buf.getvalue())
    return where, len(companies)


def match_company(text: str, companies: list[str], register: dict[str, Client]) -> list[str]:
    """Companies a name in a message refers to (Tally name or short name, partial match)."""
    low = text.strip().lower()
    if not low:
        return []
    exact = [c for c in companies if c.lower() == low or (register.get(c) and register[c].short.lower() == low)]
    if exact:
        return exact
    return [c for c in companies if low in c.lower() or (register.get(c) and low in register[c].short.lower())]
