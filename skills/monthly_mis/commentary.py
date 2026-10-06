"""Management commentary.

The AI model receives the Monthly MIS procedure (SKILL.md) and the *computed*
figures only. If the model is unavailable, a rule-based commentary is produced
so the report is never blocked.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.llm import llm

log = logging.getLogger(__name__)


def facts(res: dict[str, Any]) -> dict[str, Any]:
    """Compact, rounded facts for the model (keeps tokens and ambiguity low)."""
    pl = {k: {p: round(v, 0) for p, v in vals.items()} for k, vals in res["pl"].items()}
    kpis = {k: {p: (None if v is None else round(v * 100, 1)) for p, v in vals.items()} for k, vals in res["kpis"].items()}
    return {
        "company": res["company"], "period": res["period"], "currency": res["currency"],
        "data_provided": res["provided"],
        "pl_aed": pl,
        "kpis_pct_of_revenue": kpis,
        "tb_check": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in res["tb_check"].items()},
        "balance_sheet": {
            "lines": {k: {p: round(x, 0) for p, x in v.items()} for k, v in res["bs"]["lines"].items()},
            "working_capital": {p: round(x, 0) for p, x in res["bs"]["Working Capital"].items()},
        },
        "budget_variances": [
            {**v, "actual": round(v["actual"]), "budget": round(v["budget"]), "variance": round(v["variance"]),
             "variance_pct": None if v["variance_pct"] is None else round(v["variance_pct"] * 100, 1)}
            for v in res["budget_variances"]
        ],
        "gl_movements_top": [
            {"account": f'{g["account_code"]} {g["account_name"]}'.strip(), "line": g["mis_line"],
             "cm": round(g["cm"]), "pm": round(g["pm"]), "change": round(g["change"]),
             "change_pct": None if g["change_pct"] is None else round(g["change_pct"] * 100, 1), "flag": g["flag"]}
            for g in res["gl_movements"][:12]
        ],
        "branches": [
            {"branch": b["branch"], "revenue_cm": round(b["revenue_cm"]), "revenue_pm": round(b["revenue_pm"]),
             "revenue_change_pct": None if b["revenue_change_pct"] is None else round(b["revenue_change_pct"] * 100, 1),
             "ebitda_cm": round(b["ebitda_cm"]),
             "ebitda_pct_cm": None if b["ebitda_pct_cm"] is None else round(b["ebitda_pct_cm"] * 100, 1)}
            for b in res["branches"]
        ],
        "exceptions": [
            {"severity": e["severity"], "area": e["area"], "item": e["item"],
             "amount": None if e["amount"] is None else round(e["amount"])}
            for e in res["exceptions"][:25]
        ],
        "exception_counts": {
            s: sum(1 for e in res["exceptions"] if e["severity"] == s) for s in ("High", "Medium", "Low")
        },
    }


def ai_commentary(res: dict[str, Any], procedure: str, persona: str, version: str) -> tuple[str, str]:
    """Return (commentary, source) where source is the model name or 'rule-based'."""
    if not llm.configured:
        return rule_based(res, version), "rule-based"
    system = (
        persona
        + "\n\nYou are executing the following skill. Follow its commentary rules exactly.\n\n"
        + procedure
    )
    prompt = (
        "Write the management commentary for this month's MIS.\n"
        "Use ONLY the figures in the JSON below; do not calculate new figures other than simple "
        "differences that are already implied. Percentages in the JSON are already in % "
        "(e.g. 62.5 means 62.5%).\n"
        "Structure: 1) Headline result (2-3 sentences). 2) Key movements (3-5 short bullet points). "
        "3) Exceptions requiring action (bullets, most serious first). 4) Closing line.\n"
        "Plain text suitable for both Excel and Synology Chat; use '-' for bullets; no markdown tables; "
        "max 350 words.\n\nDATA:\n" + json.dumps(facts(res), default=str)
    )
    try:
        r = llm.complete(system, [{"role": "user", "content": prompt}], max_tokens=1500)
        return r.text, r.model
    except Exception as exc:  # noqa: BLE001
        log.warning("AI commentary failed, using rule-based: %s", exc)
        return rule_based(res, version) + f"\n\n(AI model unavailable: {type(exc).__name__}; rule-based commentary used.)", "rule-based"


def _fmt_pct(v):
    return "n/a" if v is None else f"{v*100:.1f}%"


def rule_based(res: dict[str, Any], version: str) -> str:
    pl, k = res["pl"], res["kpis"]
    rev, ebitda = pl["Revenue"], pl["EBITDA"]
    lines = [
        f"{res['company']} - {res['period']}: revenue AED {rev['cm']:,.0f}"
        + (f" ({(rev['cm']-rev['pm'])/abs(rev['pm'])*100:+.1f}% vs previous month)" if rev["pm"] else "")
        + f", gross profit {_fmt_pct(k['Gross Profit %']['cm'])}, EBITDA AED {ebitda['cm']:,.0f}"
        f" ({_fmt_pct(k['EBITDA %']['cm'])}).",
        "",
        "Key movements:",
    ]
    for label, vals in k.items():
        if vals["cm"] is not None and vals["pm"] is not None:
            pts = (vals["cm"] - vals["pm"]) * 100
            if abs(pts) >= 0.5:
                lines.append(f"- {label}: {_fmt_pct(vals['pm'])} -> {_fmt_pct(vals['cm'])} ({pts:+.1f} pts)")
    for g in res["gl_movements"][:3]:
        lines.append(f"- {g['account_name']}: AED {g['pm']:,.0f} -> AED {g['cm']:,.0f} ({g['flag']}) - requires explanation from the accounts team")
    high = [e for e in res["exceptions"] if e["severity"] == "High"]
    med = [e for e in res["exceptions"] if e["severity"] == "Medium"]
    lines += ["", f"Exceptions: {len(high)} high, {len(med)} medium."]
    for e in (high + med)[:6]:
        lines.append(f"- [{e['severity']}] {e['item']}")
    lines += ["", f"Prepared by ACE, Accountability's Chief Examiner (Monthly MIS Skill v{version}) - for review by Finance before distribution."]
    return "\n".join(lines)
