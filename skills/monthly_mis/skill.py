"""Monthly MIS skill - orchestration."""
from __future__ import annotations

import time
from pathlib import Path

import yaml

from app.config import ROOT_DIR, settings
from skills.base import SkillResult
from skills.monthly_mis import commentary as cm
from skills.monthly_mis.engine import MISInputError, analyse, load_input
from skills.monthly_mis.report import write_report

SKILL_DIR = Path(__file__).resolve().parent
MAPPING_PATH = ROOT_DIR / "config" / "coa_mapping.csv"


def _cfg() -> dict:
    return yaml.safe_load((SKILL_DIR / "skill.yaml").read_text(encoding="utf-8"))


def resolve_inbox_file(name: str, inbox: Path) -> Path:
    """Only files directly inside the inbox may be read (no paths, no traversal)."""
    name = name.strip().strip('"').strip("'")
    candidate = (inbox / Path(name).name).resolve()
    if candidate.parent != inbox.resolve():
        raise MISInputError("Only files in the inbox folder can be used.")
    if candidate.exists():
        return candidate
    # forgiving match: case-insensitive, extension optional, unique prefix
    files = [f for f in inbox.iterdir() if f.is_file() and f.suffix.lower() in (".xlsx", ".xlsm", ".xls")]
    low = name.lower()
    exact = [f for f in files if f.name.lower() == low or f.stem.lower() == low]
    if exact:
        return exact[0]
    partial = [f for f in files if low in f.name.lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise MISInputError("More than one file matches: " + ", ".join(f.name for f in partial[:5]))
    raise MISInputError(f"No file called '{name}' in the inbox. Send `files` to see what is there.")


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


def run(file_name: str, requested_by: str = "unknown", persona: str = "", use_ai: bool = True,
        inbox: Path | None = None, reports: Path | None = None) -> SkillResult:
    t0 = time.monotonic()
    cfg = _cfg()
    version = str(cfg.get("version"))
    inbox = inbox or settings.inbox_dir
    reports = reports or settings.reports_dir
    try:
        path = resolve_inbox_file(file_name, inbox)
        inp = load_input(path, cfg, MAPPING_PATH)
        res = analyse(inp, cfg)
    except MISInputError as exc:
        return SkillResult(ok=False, chat_summary=f"⚠️ Could not prepare the MIS: {exc}")

    procedure = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    if use_ai:
        text, source = cm.ai_commentary(res, procedure, persona, version)
    else:
        text, source = cm.rule_based(res, version), "rule-based"

    res["_tb"] = inp.tb
    report = write_report(res, text, source, reports, version, requested_by)
    try:
        rel = str(report.relative_to(settings.data_dir))
    except ValueError:
        rel = report.name
    summary = chat_summary(res, rel, version, time.monotonic() - t0, source)
    res.pop("_tb", None)
    return SkillResult(ok=True, chat_summary=summary, report_path=report,
                       details={"commentary": text, "commentary_source": source,
                                "exceptions": len(res["exceptions"]), "company": res["company"], "period": res["period"]})
