"""Monthly MIS skill - orchestration."""
from __future__ import annotations

import time
from pathlib import Path

import yaml

from app.config import ROOT_DIR, settings
from skills.base import SkillResult
from skills.monthly_mis import commentary as cm
from app.files import FileAccessError, resolve_file
from skills.monthly_mis.engine import MISInputError, analyse, load_input
from skills.monthly_mis.report import write_report

SKILL_DIR = Path(__file__).resolve().parent
MAPPING_PATH = ROOT_DIR / "config" / "coa_mapping.csv"


def _cfg() -> dict:
    return yaml.safe_load((SKILL_DIR / "skill.yaml").read_text(encoding="utf-8"))


def _pct(v):
    return "n/a" if v is None else f"{v*100:.1f}%"


def chat_summary(res: dict, report_rel: str, version: str, seconds: float, ai_source: str) -> str:
    pl, k = res["pl"], res["kpis"]
    rev, gp, eb = pl["Revenue"], pl["Gross Profit"], pl["EBITDA"]
    cnt = {s: sum(1 for e in res["exceptions"] if e["severity"] == s) for s in ("High", "Medium", "Low")}
    out = [
        f"✅ {res['company']} - {res['period']} MIS completed (Monthly MIS Skill v{version})",
        "",
        f"Revenue: AED {rev['cm']:,.0f}" + (f"  ({(rev['cm']-rev['pm'])/abs(rev['pm'])*100:+.1f}% vs PM)" if rev["pm"] else ""),
        f"Gross profit: AED {gp['cm']:,.0f}  ({_pct(k['Gross Profit %']['cm'])})",
        f"EBITDA: AED {eb['cm']:,.0f}  ({_pct(k['EBITDA %']['cm'])})",
        f"Net profit: AED {pl['Net Profit']['cm']:,.0f}",
        f"TB check: {'balanced ✓' if res['tb_check']['balanced_cm'] else 'OUT OF BALANCE ✗'}",
        "",
        f"Major exceptions ({cnt['High']} high, {cnt['Medium']} medium, {cnt['Low']} low):",
    ]
    top = [e for e in res["exceptions"] if e["severity"] in ("High", "Medium")][:6]
    out += [f"- [{e['severity']}] {e['item']}" for e in top] or ["- None"]
    out += [
        f"- {len(res['gl_movements'])} unusual GL movements identified",
        "",
        f"Report: {report_rel}",
        f"Commentary: {ai_source} · {seconds:.0f}s · draft for your review",
    ]
    return "\n".join(out)


def run(file_ref: str, requested_by: str = "unknown", persona: str = "", use_ai: bool = True,
        reports: Path | None = None) -> SkillResult:
    """`file_ref` is a source path such as `Clients/Mara/2026/09/TB.xlsx` or an inbox file name."""
    t0 = time.monotonic()
    cfg = _cfg()
    version = str(cfg.get("version"))
    reports = reports or settings.reports_dir
    try:
        resolved = resolve_file(file_ref)
        inp = load_input(resolved.path, cfg, MAPPING_PATH)
        inp.source_file = resolved.ref
        res = analyse(inp, cfg)
    except (MISInputError, FileAccessError) as exc:
        return SkillResult(ok=False, chat_summary=f"⚠️ Could not prepare the MIS: {exc}")

    procedure = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    if use_ai:
        text, source = cm.ai_commentary(res, procedure, persona, version)
    else:
        text, source = cm.rule_based(res, version), "rule-based"

    res["_tb"] = inp.tb
    report = write_report(res, text, source, reports, version, requested_by)
    summary = chat_summary(res, str(report), version, time.monotonic() - t0, source)
    res.pop("_tb", None)
    return SkillResult(ok=True, chat_summary=summary, report_path=report,
                       details={"source": resolved.ref, "commentary": text, "commentary_source": source,
                                "exceptions": len(res["exceptions"]), "company": res["company"], "period": res["period"]})
