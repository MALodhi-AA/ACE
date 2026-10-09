"""Read-only client for TallyPrime's XML server (Help > Settings > Connectivity, port 9000).

Tally's XML port can also import (create / alter / delete) data, and Tally itself has no
read-only switch for it. So every request goes through `check_read_only()` first: only
"Export" requests are sent, and inline TDL may only define collections (no functions,
actions or imports). Anything else is refused here, before it leaves ACE.

Amounts: in Tally's XML a debit is negative and a credit positive. `amount()` keeps that
sign; `dr_cr()` turns it into the "1,234.00 Dr" form accountants read.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from xml.sax.saxutils import escape

import httpx

DEFAULT_URL = "http://host.docker.internal:9000"   # Tally runs on the HP server itself


class TallyError(RuntimeError):
    pass


class NotReadOnly(TallyError):
    pass


def settings_from_env() -> dict:
    return {"url": os.getenv("TALLY_URL", DEFAULT_URL).rstrip("/"),
            "timeout": float(os.getenv("TALLY_TIMEOUT", "120"))}


def configured() -> bool:
    return bool(os.getenv("TALLY_URL", "").strip()) or os.getenv("TALLY_ENABLED", "").lower() in ("1", "true", "yes")


# --- read-only guard ----------------------------------------------------------------------
_FORBIDDEN_TAGS = {"IMPORTDATA", "REQUESTDATA", "TALLYMESSAGE", "FUNCTION", "ACTION", "OBJECT", "MENU",
                   "FORM", "BUTTON", "KEY"}
_FORBIDDEN_WORDS = re.compile(r"\b(import|execute|delete|alter|create|modify|cancel)\b", re.IGNORECASE)
_ALLOWED_TDL = {"COLLECTION", "SYSTEM"}          # definitions allowed inside <TDLMESSAGE>


def check_read_only(xml: str) -> None:
    """Raise NotReadOnly unless this request can only read data."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise NotReadOnly(f"request is not valid XML: {exc}") from exc
    req = (root.findtext("HEADER/TALLYREQUEST") or "").strip().lower()
    if req != "export":
        raise NotReadOnly(f"only Export requests are allowed (got {req or 'none'!r})")
    for el in root.iter():
        if el.tag.upper() in _FORBIDDEN_TAGS:
            raise NotReadOnly(f"<{el.tag}> is not allowed")
    for msg in root.iter("TDLMESSAGE"):
        for child in msg:
            if child.tag.upper() not in _ALLOWED_TDL:
                raise NotReadOnly(f"TDL <{child.tag}> is not allowed (collections only)")
    # names/values may legitimately contain these words; check only the request's structure
    skeleton = re.sub(r">[^<]*<", "><", xml)
    skeleton = re.sub(r'"[^"]*"', '""', skeleton)
    if _FORBIDDEN_WORDS.search(skeleton):
        raise NotReadOnly(f"request structure contains a write keyword: {_FORBIDDEN_WORDS.search(skeleton).group(0)}")


# --- helpers ------------------------------------------------------------------------------
_BAD_CHARREF = re.compile(r"&#(\d+);")


def _clean(text: str) -> str:
    """Tally sometimes sends control characters (&#4; etc.) that are not valid XML."""
    def fix(m):
        n = int(m.group(1))
        return m.group(0) if n in (9, 10, 13) or n >= 32 else ""
    text = _BAD_CHARREF.sub(fix, text)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if len(raw) > 1 and raw[1:2] == b"\x00":
        return raw.decode("utf-16-le")
    return raw.decode("utf-8", errors="replace")


def tally_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def tally_day(d: date) -> str:
    """1-Apr-2026 - the form Tally's date variables accept."""
    return f"{d.day}-{d:%b-%Y}"


def parse_tally_date(s: str | None) -> date | None:
    s = (s or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    for fmt in ("%d-%b-%Y", "%d-%b-%y"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def amount(s: str | None) -> float:
    """'-1,234.50' / '1234.50 Dr' / '' -> float (debit negative, as Tally sends it)."""
    s = (s or "").strip().replace(",", "")
    if not s:
        return 0.0
    m = re.match(r"^(-?[\d.]+)\s*(Dr|Cr)?", s, re.IGNORECASE)
    if not m:
        # foreign currency values look like '$ 100.00 @ ... = -367.00' - take the last number
        nums = re.findall(r"-?[\d.]+", s)
        return float(nums[-1]) if nums else 0.0
    v = float(m.group(1))
    if m.group(2):
        v = -abs(v) if m.group(2).lower() == "dr" else abs(v)
    return v


def dr_cr(v: float) -> str:
    if abs(v) < 0.005:
        return "0.00"
    return f"{abs(v):,.2f} {'Dr' if v < 0 else 'Cr'}"


@dataclass
class Ledger:
    name: str
    parent: str
    opening: float
    closing: float


# --- client -------------------------------------------------------------------------------
class Tally:
    def __init__(self, url: str | None = None, timeout: float | None = None, transport=None) -> None:
        cfg = settings_from_env()
        self.url = (url or cfg["url"]).rstrip("/")
        self.timeout = timeout or cfg["timeout"]
        self._http = httpx.Client(timeout=self.timeout, transport=transport)

    def request(self, xml: str) -> ET.Element:
        check_read_only(xml)                                   # never send anything that could write
        try:
            r = self._http.post(self.url, content=xml.encode("utf-8"),
                                headers={"Content-Type": "text/xml; charset=utf-8"})
        except httpx.HTTPError as exc:
            raise TallyError(f"cannot reach Tally at {self.url}: {exc}. Is TallyPrime open with "
                             "Connectivity set to Server/Both on port 9000?") from exc
        text = _clean(_decode(r.content))
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise TallyError(f"Tally sent something that is not XML: {text[:200]!r}") from exc
        err = root.findtext(".//LINEERROR")
        if err:
            raise TallyError(f"Tally: {err.strip()}")
        return root

    @staticmethod
    def _envelope(kind: str, ident: str, company: str | None = None, frm: date | None = None,
                  to: date | None = None, tdl: str = "", extra: dict | None = None) -> str:
        sv = {"SVEXPORTFORMAT": "$$SysName:XML"}
        if company:
            sv["SVCURRENTCOMPANY"] = company
        sv.update(extra or {})
        svx = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in sv.items())
        # v0.7.5: Tally applies a period only when the variables are typed as dates
        if frm:
            svx += f'<SVFROMDATE TYPE="Date">{tally_day(frm)}</SVFROMDATE>'
        if to:
            svx += f'<SVTODATE TYPE="Date">{tally_day(to)}</SVTODATE>'
        tdlx = f"<TDL><TDLMESSAGE>{tdl}</TDLMESSAGE></TDL>" if tdl else ""
        return (f"<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
                f"<TYPE>{kind}</TYPE><ID>{escape(ident)}</ID></HEADER><BODY><DESC>"
                f"<STATICVARIABLES>{svx}</STATICVARIABLES>{tdlx}</DESC></BODY></ENVELOPE>")

    def collection(self, name: str, obj_type: str, fetch: list[str], company: str | None = None,
                   frm: date | None = None, to: date | None = None, child_of: str | None = None,
                   formulas: dict[str, str] | None = None) -> list[ET.Element]:
        """Objects of one type with the fields asked for (inline TDL collection, read-only).
        formulas: {name: TDL condition} used as filters, e.g. {"InPeriod": '$Date >= $$Date:"1-Jun-2026"'}."""
        parts = [f"<TYPE>{escape(obj_type)}</TYPE>"]
        if child_of:
            parts.append(f"<CHILDOF>{escape(child_of)}</CHILDOF>")
        parts.append(f"<FETCH>{escape(', '.join(fetch))}</FETCH>")
        for f in formulas or {}:
            parts.append(f"<FILTERS>{escape(f)}</FILTERS>")
        tdl = f'<COLLECTION NAME="{escape(name)}" ISMODIFY="No">{"".join(parts)}</COLLECTION>'
        for f, cond in (formulas or {}).items():
            tdl += f'<SYSTEM TYPE="Formulae" NAME="{escape(f)}">{escape(cond)}</SYSTEM>'
        root = self.request(self._envelope("Collection", name, company, frm, to, tdl))
        tag = obj_type.upper()
        return list(root.iter(tag))

    # --- read functions ---------------------------------------------------------------
    def companies(self) -> list[dict]:
        """Companies open (loaded) in Tally."""
        rows = self.collection("ACECompanies", "Company", ["Name", "StartingFrom", "BooksFrom"])
        out = []
        for c in rows:
            name = c.get("NAME") or c.findtext("NAME") or ""
            out.append({"name": name.strip(),
                        "starting_from": parse_tally_date(c.findtext("STARTINGFROM")),
                        "books_from": parse_tally_date(c.findtext("BOOKSFROM"))})
        return [c for c in out if c["name"]]

    def ledgers(self, company: str, frm: date | None = None, to: date | None = None) -> list[Ledger]:
        """Every ledger with its group, opening and closing balance (for the period given)."""
        rows = self.collection("ACELedgers", "Ledger", ["Name", "Parent", "OpeningBalance", "ClosingBalance"],
                               company=company, frm=frm, to=to)
        out = []
        for l in rows:
            name = (l.get("NAME") or l.findtext("NAME") or "").strip()
            if name:
                out.append(Ledger(name, (l.findtext("PARENT") or "").strip(),
                                  amount(l.findtext("OPENINGBALANCE")), amount(l.findtext("CLOSINGBALANCE"))))
        return out

    def voucher_dates(self, company: str, frm: date | None = None, to: date | None = None,
                      strict: bool = True, extra: dict[str, str] | None = None) -> list[date]:
        """Dates of the vouchers in the period. strict: the period is also written into the request as a
        filter, so it holds even if Tally keeps its own selected period."""
        formulas = {}
        if strict and frm:
            formulas["ACEFrom"] = f'$Date >= $$Date:"{tally_day(frm)}"'
        if strict and to:
            formulas["ACETo"] = f'$Date <= $$Date:"{tally_day(to)}"'
        formulas.update(extra or {})
        rows = self.collection("ACEVoucherDates", "Voucher", ["Date"], company=company, frm=frm, to=to,
                               formulas=formulas)
        return sorted(d for d in (parse_tally_date(v.findtext("DATE")) for v in rows) if d)

    def last_voucher_date(self, company: str, frm: date | None = None, to: date | None = None) -> date | None:
        """Date of the latest voucher in the period (books up to date?)."""
        dates = self.voucher_dates(company, frm, to)
        return dates[-1] if dates else None

    def report(self, company: str, report: str, frm: date | None = None, to: date | None = None,
               extra: dict | None = None) -> ET.Element:
        """One of Tally's own reports as XML (e.g. 'Trial Balance', 'Balance Sheet', 'Profit and Loss')."""
        return self.request(self._envelope("Data", report, company, frm, to, extra=extra))

    def trial_balance(self, company: str, frm: date | None = None, to: date | None = None) -> list[tuple[str, float]]:
        """Tally's own Trial Balance (top level, as on screen): [(name, closing)] with debit negative."""
        root = self.report(company, "Trial Balance", frm, to)
        rows, name = [], None
        for el in root:
            if el.tag == "DSPACCNAME":
                name = (el.findtext("DSPDISPNAME") or "").strip()
            elif el.tag == "DSPACCINFO" and name is not None:
                dr = amount(el.findtext("DSPCLDRAMT/DSPCLDRAMTA"))
                cr = amount(el.findtext("DSPCLCRAMT/DSPCLCRAMTA"))
                rows.append((name, -abs(dr) + abs(cr)))
                name = None
        return rows

    # --- v0.8.0: groups and vouchers with their ledger lines ------------------------------
    def groups(self, company: str) -> dict[str, str]:
        """{group name: parent group} ('' for the primary groups)."""
        rows = self.collection("ACEGroups", "Group", ["Name", "Parent"], company=company)
        out = {}
        for g in rows:
            name = (g.get("NAME") or g.findtext("NAME") or "").strip()
            if name:
                out[name] = (g.findtext("PARENT") or "").strip()
        return out

    def vouchers(self, company: str, frm: date, to: date) -> list[dict]:
        """Vouchers in the period with their ledger lines:
        [{date, number, type, narration, lines: [(ledger, amount)]}] - amount debit negative."""
        formulas = {"ACEFrom": f'$Date >= $$Date:"{tally_day(frm)}"', "ACETo": f'$Date <= $$Date:"{tally_day(to)}"'}
        rows = self.collection("ACEVouchers", "Voucher",
                               ["Date", "VoucherNumber", "VoucherTypeName", "Narration", "PartyLedgerName",
                                "AllLedgerEntries.LedgerName", "AllLedgerEntries.Amount",
                                "LedgerEntries.LedgerName", "LedgerEntries.Amount"],
                               company=company, frm=frm, to=to, formulas=formulas)
        out = []
        for v in rows:
            lines = []
            for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
                for e in v.iter(tag):
                    name = (e.findtext("LEDGERNAME") or "").strip()
                    if name:
                        lines.append((name, amount(e.findtext("AMOUNT"))))
                if lines:
                    break
            out.append({"date": parse_tally_date(v.findtext("DATE")),
                        "number": (v.findtext("VOUCHERNUMBER") or "").strip(),
                        "type": (v.findtext("VOUCHERTYPENAME") or "").strip(),
                        "narration": (v.findtext("NARRATION") or "").strip(),
                        "party": (v.findtext("PARTYLEDGERNAME") or "").strip(),
                        "lines": lines})
        return out


def in_group(group: str, target: str, groups: dict[str, str]) -> bool:
    """Is `group` the target group or below it?"""
    target = target.strip().lower()
    seen = set()
    g = group
    while g and g.lower() not in seen:
        if g.lower() == target:
            return True
        seen.add(g.lower())
        g = next((p for n, p in groups.items() if n.lower() == g.lower()), "")
    return False
