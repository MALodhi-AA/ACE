"""Monthly MIS - deterministic calculation engine.

All figures in the MIS are produced here, in plain Python/pandas, so that they
are reproducible and testable. The AI model never does arithmetic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

PERIODS = ["cm", "pm", "budget", "ytd", "ytd_py"]
PERIOD_LABELS = {
    "cm": "Current Month",
    "pm": "Previous Month",
    "budget": "Budget",
    "ytd": "YTD",
    "ytd_py": "YTD Prior Year",
}

# Column header aliases accepted in the TB sheet (lower-case, spaces collapsed)
HEADER_ALIASES = {
    "account_code": ["account code", "code", "gl code", "ledger code", "account no", "account number"],
    "account_name": ["account name", "account", "ledger", "ledger name", "gl name", "particulars", "description"],
    "mis_line": ["mis line", "mis group", "mis", "category", "group"],
    "branch": ["branch", "cost centre", "cost center", "location", "outlet"],
    "cm": ["current month", "cm", "actual", "month", "this month"],
    "pm": ["previous month", "pm", "prior month", "last month"],
    "budget": ["budget", "budget month", "bud"],
    "ytd": ["ytd", "ytd actual", "year to date"],
    "ytd_py": ["ytd prior year", "ytd py", "prior year ytd", "py ytd", "last year ytd"],
}

# Presentation sign: income lines are credits (negative in TB) shown positive.
CREDIT_NATURE_PL = {"Revenue", "Other Income"}
CREDIT_NATURE_BS = {"Current Liabilities", "Non-Current Liabilities", "Equity"}
ASSET_LINES = {"Non-Current Assets", "Cash", "Current Assets"}

SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}


class MISInputError(ValueError):
    """Raised when the input file cannot be used; message is shown to the user."""


@dataclass
class MISInput:
    company: str
    period: str
    currency: str
    tb: pd.DataFrame
    source_file: str
    has_branches: bool
    provided: dict[str, bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _read_info(xls: pd.ExcelFile) -> dict[str, str]:
    info: dict[str, str] = {}
    sheet = next((n for n in xls.sheet_names if _norm(n) == "info"), None)
    if not sheet:
        return info
    df = pd.read_excel(xls, sheet_name=sheet, header=None)
    for _, row in df.iterrows():
        if len(row) >= 2 and pd.notna(row.iloc[0]) and pd.notna(row.iloc[1]):
            info[_norm(row.iloc[0])] = str(row.iloc[1]).strip()
    return info


def _find_header_row(raw: pd.DataFrame) -> int:
    """Allow a few title rows above the header (common in exported reports)."""
    wanted = {a for aliases in HEADER_ALIASES.values() for a in aliases}
    for i in range(min(15, len(raw))):
        cells = {_norm(v) for v in raw.iloc[i].tolist()}
        if len(cells & wanted) >= 3:
            return i
    raise MISInputError("Could not find the TB header row (expected columns like 'Account Name', 'Current Month').")


def load_mapping(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    if not path.exists():
        return {}, {}
    df = pd.read_csv(path, dtype=str).fillna("")
    by_code = {r["account_code"].strip(): r["mis_line"].strip() for _, r in df.iterrows() if r.get("account_code", "").strip()}
    by_name = {_norm(r["account_name"]): r["mis_line"].strip() for _, r in df.iterrows() if r.get("account_name", "").strip()}
    return by_code, by_name


def load_input(path: Path, cfg: dict, mapping_path: Path | None = None) -> MISInput:
    if not path.exists():
        raise MISInputError(f"File not found: {path.name}")
    try:
        xls = pd.ExcelFile(path)
    except Exception as exc:  # noqa: BLE001
        raise MISInputError(f"Could not open {path.name} as Excel: {exc}") from exc

    info = _read_info(xls)
    tb_sheet = next((n for n in xls.sheet_names if _norm(n) in {"tb", "trial balance"}), xls.sheet_names[0])
    raw = pd.read_excel(xls, sheet_name=tb_sheet, header=None)
    hdr = _find_header_row(raw)
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [_norm(c) for c in raw.iloc[hdr].tolist()]

    rename: dict[str, str] = {}
    for key, aliases in HEADER_ALIASES.items():
        for col in df.columns:
            if col in aliases and col not in rename and key not in rename.values():
                rename[col] = key
    df = df.rename(columns=rename)
    df = df[[c for c in df.columns if c in HEADER_ALIASES]]

    if "account_name" not in df.columns:
        raise MISInputError("TB sheet needs an 'Account Name' column.")
    if "cm" not in df.columns:
        raise MISInputError("TB sheet needs a 'Current Month' column.")

    provided = {p: p in df.columns for p in PERIODS}
    for col in ["account_code", "mis_line", "branch"]:
        if col not in df.columns:
            df[col] = ""
    for p in PERIODS:
        if p not in df.columns:
            df[p] = 0.0
        df[p] = pd.to_numeric(df[p], errors="coerce").fillna(0.0).astype(float)

    df["account_name"] = df["account_name"].fillna("").astype(str).str.strip()
    df["account_code"] = df["account_code"].fillna("").astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    df["branch"] = df["branch"].fillna("").astype(str).str.strip()
    df = df[df["account_name"] != ""]
    # drop total rows carried over from exports
    df = df[~df["account_name"].str.lower().str.match(r"^(grand )?total\b")]
    if df.empty:
        raise MISInputError("TB sheet has no account rows.")

    # --- map MIS lines ------------------------------------------------------
    canonical = {_norm(x): x for x in cfg["pl_lines"] + cfg["bs_lines"]}
    by_code, by_name = load_mapping(mapping_path) if mapping_path else ({}, {})

    def resolve(row) -> str:
        for candidate in (row.get("mis_line"), by_code.get(row["account_code"]), by_name.get(_norm(row["account_name"]))):
            if candidate is not None and _norm(candidate) in canonical:
                return canonical[_norm(candidate)]
        return ""

    df["mis_line"] = df.apply(resolve, axis=1)
    df["section"] = df["mis_line"].map(
        lambda x: "PL" if x in cfg["pl_lines"] else ("BS" if x in cfg["bs_lines"] else "UNMAPPED")
    )
    has_branches = df["branch"].replace("", pd.NA).dropna().nunique() > 1
    df.loc[df["branch"] == "", "branch"] = "Unallocated" if has_branches else "All"

    company = info.get("company") or path.stem.split("_")[0]
    period = info.get("period") or ""
    currency = info.get("currency") or "AED"
    return MISInput(company, period, currency, df.reset_index(drop=True), path.name, has_branches, provided)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _pct(n: float, d: float) -> float | None:
    return None if abs(d) < 1e-9 else n / d


def _pl_sign(line: str) -> int:
    return -1 if line in CREDIT_NATURE_PL else 1


def build_pl(df: pd.DataFrame, cfg: dict) -> dict[str, dict[str, float]]:
    pl_rows = df[df["section"] == "PL"]
    lines: dict[str, dict[str, float]] = {}
    for line in cfg["pl_lines"]:
        sub = pl_rows[pl_rows["mis_line"] == line]
        lines[line] = {p: float(sub[p].sum()) * _pl_sign(line) for p in PERIODS}

    def calc(fn):
        return {p: fn(p) for p in PERIODS}

    L = lines
    lines["Gross Profit"] = calc(lambda p: L["Revenue"][p] - L["Cost of Sales"][p])
    lines["EBITDA"] = calc(
        lambda p: L["Gross Profit"][p] - L["Payroll"][p] - L["Rent"][p]
        - L["Other Operating Expenses"][p] + L["Other Income"][p]
    )
    lines["EBIT"] = calc(lambda p: L["EBITDA"][p] - L["Depreciation"][p])
    lines["Profit Before Tax"] = calc(lambda p: L["EBIT"][p] - L["Finance Costs"][p])
    lines["Net Profit"] = calc(lambda p: L["Profit Before Tax"][p] - L["Tax"][p])
    return lines


PL_LAYOUT = [
    ("Revenue", "line"), ("Cost of Sales", "line"), ("Gross Profit", "subtotal"),
    ("Payroll", "line"), ("Rent", "line"), ("Other Operating Expenses", "line"),
    ("Other Income", "line"), ("EBITDA", "subtotal"), ("Depreciation", "line"),
    ("EBIT", "subtotal"), ("Finance Costs", "line"), ("Profit Before Tax", "subtotal"),
    ("Tax", "line"), ("Net Profit", "total"),
]


def build_kpis(pl: dict, cfg: dict) -> dict[str, dict[str, float | None]]:
    rev = pl["Revenue"]
    defs = [
        ("Gross Profit %", "Gross Profit"),
        (cfg.get("cost_of_sales_label", "Cost of Sales %"), "Cost of Sales"),
        ("Payroll %", "Payroll"),
        ("Rent %", "Rent"),
        ("EBITDA %", "EBITDA"),
        ("Net Profit %", "Net Profit"),
    ]
    return {label: {p: _pct(pl[line][p], rev[p]) for p in PERIODS} for label, line in defs}


def build_bs(df: pd.DataFrame, pl: dict, cfg: dict) -> dict[str, Any]:
    bs_rows = df[df["section"] == "BS"]
    out: dict[str, dict[str, float]] = {}
    for line in cfg["bs_lines"]:
        sub = bs_rows[bs_rows["mis_line"] == line]
        sign = -1 if line in CREDIT_NATURE_BS else 1
        out[line] = {"cm": float(sub["cm"].sum()) * sign, "pm": float(sub["pm"].sum()) * sign}
    cyp = {"cm": pl["Net Profit"]["ytd"], "pm": pl["Net Profit"]["ytd"] - pl["Net Profit"]["cm"]}
    out["Current Year Profit"] = cyp
    res: dict[str, Any] = {"lines": out}
    for p in ("cm", "pm"):
        assets = sum(out[l][p] for l in ASSET_LINES)
        liabs = out["Current Liabilities"][p] + out["Non-Current Liabilities"][p]
        equity = out["Equity"][p] + cyp[p]
        res.setdefault("Total Assets", {})[p] = assets
        res.setdefault("Total Liabilities", {})[p] = liabs
        res.setdefault("Total Equity", {})[p] = equity
        res.setdefault("Difference", {})[p] = assets - liabs - equity
        res.setdefault("Working Capital", {})[p] = out["Current Assets"][p] + out["Cash"][p] - out["Current Liabilities"][p]
    return res


def tb_check(df: pd.DataFrame, cfg: dict) -> dict[str, Any]:
    tol = cfg["thresholds"]["tb_balance_tolerance"]
    bs = df[df["section"] == "BS"]
    plr = df[df["section"] == "PL"]
    unm = df[df["section"] == "UNMAPPED"]
    # Current month end: BS closing (== YTD col) + P&L YTD; all rows use the YTD column.
    diff_cm = float(df["ytd"].sum())
    # Previous month end: BS previous closing + P&L YTD less current month (mapped rows only)
    diff_pm = float(bs["pm"].sum() + (plr["ytd"] - plr["cm"]).sum())
    return {
        "diff_cm": diff_cm,
        "diff_pm": diff_pm,
        "balanced_cm": abs(diff_cm) <= tol,
        "balanced_pm": abs(diff_pm) <= tol,
        "pm_check_complete": unm.empty,
        "accounts": int(len(df)),
        "unmapped": int(len(unm)),
    }


def gl_movements(df: pd.DataFrame, cfg: dict) -> list[dict[str, Any]]:
    th = cfg["thresholds"]
    plr = df[df["section"] == "PL"]
    if plr.empty:
        return []
    g = plr.groupby(["account_code", "account_name", "mis_line"], as_index=False)[["cm", "pm", "budget"]].sum()
    out = []
    for _, r in g.iterrows():
        s = _pl_sign(r["mis_line"])
        cm, pm, bud = r["cm"] * s, r["pm"] * s, r["budget"] * s
        diff = cm - pm
        pct = _pct(diff, abs(pm))
        if abs(pm) < 1e-9 and abs(cm) >= th["gl_movement_abs"]:
            flag = "New balance this month"
        elif abs(cm) < 1e-9 and abs(pm) >= th["gl_movement_abs"]:
            flag = "Nil this month (balance last month)"
        elif abs(diff) >= th["gl_movement_abs"] and pct is not None and abs(pct) >= th["gl_movement_pct"]:
            flag = "Increase" if diff > 0 else "Decrease"
        else:
            continue
        out.append({
            "account_code": r["account_code"], "account_name": r["account_name"], "mis_line": r["mis_line"],
            "cm": cm, "pm": pm, "change": diff, "change_pct": pct, "budget": bud, "flag": flag,
        })
    out.sort(key=lambda x: abs(x["change"]), reverse=True)
    return out[: th["gl_movement_top_n"]]


def branch_performance(df: pd.DataFrame, cfg: dict) -> list[dict[str, Any]]:
    plr = df[df["section"] == "PL"]
    rows = []
    for br, sub in plr.groupby("branch"):
        pl = build_pl(sub, cfg)
        rev = pl["Revenue"]
        rows.append({
            "branch": br,
            "revenue_cm": rev["cm"], "revenue_pm": rev["pm"], "revenue_budget": rev["budget"],
            "revenue_change_pct": _pct(rev["cm"] - rev["pm"], abs(rev["pm"])),
            "revenue_vs_budget_pct": _pct(rev["cm"] - rev["budget"], abs(rev["budget"])),
            "gp_cm": pl["Gross Profit"]["cm"], "gp_pct_cm": _pct(pl["Gross Profit"]["cm"], rev["cm"]),
            "gp_pct_pm": _pct(pl["Gross Profit"]["pm"], rev["pm"]),
            "ebitda_cm": pl["EBITDA"]["cm"], "ebitda_pm": pl["EBITDA"]["pm"], "ebitda_budget": pl["EBITDA"]["budget"],
            "ebitda_pct_cm": _pct(pl["EBITDA"]["cm"], rev["cm"]),
        })
    rows.sort(key=lambda r: r["revenue_cm"], reverse=True)
    return rows


def _has_kw(name: str, words: list[str]) -> bool:
    n = name.lower()
    return any(w.lower() in n for w in words)


def exceptions(inp: MISInput, res: dict, cfg: dict) -> list[dict[str, Any]]:
    th, kw = cfg["thresholds"], cfg["keywords"]
    df = inp.tb
    ex: list[dict[str, Any]] = []

    def add(sev, area, item, detail, amount=None):
        ex.append({"severity": sev, "area": area, "item": item, "detail": detail, "amount": amount})

    tbc = res["tb_check"]
    if not tbc["balanced_cm"]:
        add("High", "Data integrity", "Trial balance does not balance (current month end)",
            "Balance sheet closing balances + P&L YTD do not net to zero. Figures may be incomplete.", tbc["diff_cm"])
    if inp.provided.get("pm") and tbc["pm_check_complete"] and not tbc["balanced_pm"]:
        add("High", "Data integrity", "Trial balance does not balance (previous month end)",
            "Previous month BS balances + (P&L YTD - current month) do not net to zero.", tbc["diff_pm"])
    bsd = res["bs"]["Difference"]["cm"]
    if abs(bsd) > th["tb_balance_tolerance"] and tbc["balanced_cm"] and tbc["unmapped"] == 0:
        add("High", "Data integrity", "Balance sheet does not balance",
            "Assets - liabilities - equity - current year profit is not zero; check MIS line mapping.", bsd)

    for _, r in df[df["section"] == "UNMAPPED"].iterrows():
        add("High", "Data integrity", f"Unmapped account: {r['account_code']} {r['account_name']}".strip(),
            "No valid MIS line - excluded from P&L/BS. Add to config/coa_mapping.csv or the MIS Line column.", r["ytd"] or r["cm"])

    for _, r in df.iterrows():
        name, line, code = r["account_name"], r["mis_line"], r["account_code"]
        label = f"{code} {name}".strip() + (f" [{r['branch']}]" if inp.has_branches and r["section"] == "PL" else "")
        bal = r["cm"]
        if r["section"] == "BS" and abs(bal) > th["tb_balance_tolerance"]:
            if _has_kw(name, kw["suspense"]):
                add("High", "Accounting", f"Suspense / clearing balance: {label}", "Clear or reallocate before closing the month.", bal)
            elif _has_kw(name, kw["intercompany"]):
                add("Medium", "Accounting", f"Intercompany / related party balance: {label}",
                    "Confirm reconciled with the counterparty and matching balance.", bal)
            if line == "Cash" and bal < 0:
                add("High", "Accounting", f"Negative cash / bank balance: {label}", "Credit balance on a cash or bank account.", bal)
            elif line in ("Current Assets", "Non-Current Assets") and bal < 0:
                add("Medium", "Accounting", f"Asset with credit balance: {label}", "Check for misposting or reclassify.", bal)
            elif line in ("Current Liabilities", "Non-Current Liabilities") and bal > 0:
                add("Medium", "Accounting", f"Liability with debit balance: {label}", "Check for misposting or reclassify.", bal)
        if r["section"] == "PL" and abs(bal) > th["tb_balance_tolerance"]:
            if line in CREDIT_NATURE_PL and bal > 0:
                add("Medium", "Accounting", f"Income account with debit movement: {label}", "Reversal, refund or misposting?", bal)
            elif line not in CREDIT_NATURE_PL and bal < 0:
                add("Medium", "Accounting", f"Expense account with credit movement: {label}", "Reversal, accrual release or misposting?", bal)
            if _has_kw(name, kw["suspense"]):
                add("High", "Accounting", f"Suspense / clearing account in P&L: {label}", "Reallocate to the correct expense or income account.", bal)

    # KPI movements
    for k, vals in res["kpis"].items():
        cm, pm, bud = vals["cm"], vals["pm"], vals["budget"]
        if cm is not None and pm is not None and inp.provided.get("pm"):
            pts = (cm - pm) * 100
            if abs(pts) >= th["kpi_change_pts"]:
                add("Medium", "Performance", f"{k} moved {pts:+.1f} pts vs previous month",
                    f"{pm*100:.1f}% → {cm*100:.1f}%.", None)
        if cm is not None and bud is not None and inp.provided.get("budget"):
            pts = (cm - bud) * 100
            if abs(pts) >= th["kpi_change_pts"]:
                add("Low", "Performance", f"{k} {pts:+.1f} pts vs budget", f"Budget {bud*100:.1f}% vs actual {cm*100:.1f}%.", None)

    for b in res["branches"]:
        ch = b["revenue_change_pct"]
        if ch is not None and ch <= -th["branch_revenue_decline_pct"] and inp.provided.get("pm"):
            add("Medium", "Performance", f"Branch revenue down {abs(ch)*100:.1f}%: {b['branch']}",
                f"AED {b['revenue_pm']:,.0f} → AED {b['revenue_cm']:,.0f}.", b["revenue_cm"] - b["revenue_pm"])

    if not inp.provided.get("budget"):
        add("Low", "Data integrity", "No budget provided", "Budget comparisons skipped.", None)
    if not inp.provided.get("ytd"):
        add("Medium", "Data integrity", "No YTD column provided", "TB balance check and YTD analysis are not reliable.", None)

    ex.sort(key=lambda e: SEVERITY_ORDER[e["severity"]])
    return ex


def budget_variances(pl: dict, cfg: dict) -> list[dict[str, Any]]:
    th = cfg["thresholds"]
    out = []
    for line, kind in PL_LAYOUT:
        cm, bud = pl[line]["cm"], pl[line]["budget"]
        diff = cm - bud
        pct = _pct(diff, abs(bud))
        if abs(diff) >= th["gl_movement_abs"] and (pct is None or abs(pct) >= th["budget_variance_pct"]):
            income_like = line in CREDIT_NATURE_PL or kind != "line"
            favourable = diff > 0 if income_like else diff < 0
            out.append({"line": line, "actual": cm, "budget": bud, "variance": diff, "variance_pct": pct,
                        "assessment": "Favourable" if favourable else "Adverse"})
    return out


def analyse(inp: MISInput, cfg: dict) -> dict[str, Any]:
    pl = build_pl(inp.tb, cfg)
    res: dict[str, Any] = {
        "company": inp.company, "period": inp.period, "currency": inp.currency, "source_file": inp.source_file,
        "provided": inp.provided, "has_branches": inp.has_branches,
        "pl": pl,
        "kpis": build_kpis(pl, cfg),
        "tb_check": tb_check(inp.tb, cfg),
        "gl_movements": gl_movements(inp.tb, cfg),
        "branches": branch_performance(inp.tb, cfg) if inp.has_branches else [],
        "budget_variances": budget_variances(pl, cfg) if inp.provided.get("budget") else [],
    }
    res["bs"] = build_bs(inp.tb, pl, cfg)
    res["exceptions"] = exceptions(inp, res, cfg)
    return res
