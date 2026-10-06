"""Generate the MIS input template and a balanced sample TB for testing.

    python tools/make_sample_data.py

Creates:
  templates/MIS_Input_Template.xlsx
  data/inbox/Sample_Restaurant_Sep2026_TB.xlsx   (fictional data with planted issues)
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

CFG = yaml.safe_load((ROOT / "skills" / "monthly_mis" / "skill.yaml").read_text())
HEADERS = ["Account Code", "Account Name", "MIS Line", "Branch", "Current Month", "Previous Month", "Budget", "YTD",
           "YTD Prior Year"]
TEAL, NAVY = "174D51", "002D49"


def _style_header(ws, row=1):
    for c in ws[row]:
        c.font = Font(name="Arial", bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", start_color=TEAL, end_color=TEAL)
        c.alignment = Alignment(wrap_text=True, vertical="center")


def _book(info: dict, rows: list[list]) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Info"
    for k, v in info.items():
        ws.append([k, v])
    for r in ws.iter_rows():
        r[0].font = Font(name="Arial", bold=True, color=NAVY)
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 40

    tb = wb.create_sheet("TB")
    tb.append(HEADERS)
    _style_header(tb)
    for r in rows:
        tb.append(r)
    for col, w in zip("ABCDEFGHI", [13, 34, 26, 18, 16, 16, 16, 16, 16]):
        tb.column_dimensions[col].width = w
    for row in tb.iter_rows(min_row=2, min_col=5, max_col=9):
        for c in row:
            c.number_format = "#,##0.00;(#,##0.00);-"
    tb.freeze_panes = "C2"

    lines = wb.create_sheet("MIS Lines")
    lines.append(["Section", "MIS Line"])
    _style_header(lines)
    for l in CFG["pl_lines"]:
        lines.append(["P&L", l])
    for l in CFG["bs_lines"]:
        lines.append(["Balance Sheet", l])
    lines.column_dimensions["A"].width = 16
    lines.column_dimensions["B"].width = 28
    n = len(CFG["pl_lines"]) + len(CFG["bs_lines"]) + 1
    dv = DataValidation(type="list", formula1=f"='MIS Lines'!$B$2:$B${n}", allow_blank=True)
    tb.add_data_validation(dv)
    dv.add("C2:C5000")
    return wb


def make_template() -> Path:
    wb = _book({"Company": "<Company name>", "Period": "Sep 2026", "Currency": "AED"}, [])
    ins = wb.create_sheet("Instructions", 0)
    text = [
        "ACE - Monthly MIS input template",
        "",
        "1. Info sheet: enter Company, Period (e.g. Sep 2026) and Currency.",
        "2. TB sheet: paste the trial balance - one row per account (per branch if you track branches).",
        "3. Sign convention: DEBIT POSITIVE, CREDIT NEGATIVE (as exported from Tally).",
        "4. P&L accounts: Current Month / Previous Month / Budget = the month's movement; YTD / YTD Prior Year = year-to-date.",
        "5. Balance sheet accounts: Current Month and YTD = closing balance at period end; Previous Month = closing balance at previous month end.",
        "6. MIS Line: choose from the drop-down (see 'MIS Lines'). Leave blank if the account is in config/coa_mapping.csv.",
        "7. Equity should exclude the current year's profit (the TB's retained earnings before closing entries).",
        "8. Save the file into the Synology Drive folder ACE > inbox, then send:  mis <file name>",
    ]
    for t in text:
        ins.append([t])
    ins["A1"].font = Font(name="Arial", bold=True, size=14, color=NAVY)
    ins.column_dimensions["A"].width = 130
    out = ROOT / "templates" / "MIS_Input_Template.xlsx"
    out.parent.mkdir(exist_ok=True)
    wb.save(out)
    return out


def make_sample(seed: int = 7) -> Path:
    rnd = random.Random(seed)
    branches = {"Dubai Mall": 1.0, "JBR": 0.75, "Al Barsha": 0.55}
    pl_accounts = [
        ("4000", "Food Sales", "Revenue", -420000),
        ("4010", "Beverage Sales", "Revenue", -95000),
        ("5000", "Food Cost", "Cost of Sales", 126000),
        ("5010", "Beverage Cost", "Cost of Sales", 19000),
        ("6000", "Salaries and Wages", "Payroll", 128000),
        ("6010", "Staff Accommodation", "", 14000),       # mapped via coa_mapping.csv
        ("6100", "Rent", "Rent", 68000),
        ("6200", "Utilities", "Other Operating Expenses", 21500),
        ("6300", "Marketing", "Other Operating Expenses", 14000),
        ("6400", "Repairs and Maintenance", "Other Operating Expenses", 4500),
        ("6500", "Delivery Commission", "Other Operating Expenses", 15500),
        ("4900", "Miscellaneous Income", "Other Income", -1500),
        ("7000", "Depreciation", "Depreciation", 18000),
        ("7100", "Bank Charges and Interest", "Finance Costs", 2200),
    ]
    rows = []
    for br, scale in branches.items():
        for code, name, line, base in pl_accounts:
            pm = base * scale * rnd.uniform(0.95, 1.05)
            cm = pm * rnd.uniform(0.97, 1.04)
            budget = base * scale * 1.02
            if br == "Dubai Mall" and line == "Revenue":
                cm = pm * 0.84                                     # planted: revenue decline
            if code == "5000":
                cm = cm * 1.16                                     # planted: food cost spike
            if br == "JBR" and code == "6400":
                cm = 42000                                         # planted: abnormal repairs
            ytd = cm + pm * 7.6
            ytd_py = ytd * rnd.uniform(0.88, 0.97)
            rows.append([code, name, line, br, round(cm, 2), round(pm, 2), round(budget, 2), round(ytd, 2), round(ytd_py, 2)])
    # planted: a new consultancy expense posted to an odd account at head office
    rows.append(["6900", "Expenses to be allocated", "Other Operating Expenses", "Dubai Mall", 23500.0, 0.0, 0.0, 23500.0, 0.0])

    pl_ytd = sum(r[7] for r in rows)
    pl_ytd_less_cm = sum(r[7] - r[4] for r in rows)

    bs = [
        ("1000", "Property Plant and Equipment", "Non-Current Assets", 1850000, 1868000),
        ("1200", "Trade Receivables", "Current Assets", 64000, 58500),
        ("1210", "Inventory", "Current Assets", 92000, 88000),
        ("1220", "Prepayments", "Current Assets", 41000, 47000),
        ("1230", "Due from Related Party", "Current Assets", 54800, 54800),   # planted: intercompany
        ("1290", "Suspense Account", "Current Assets", 12750, 0),             # planted: suspense
        ("1110", "Petty Cash", "Cash", 3500, 3200),
        ("2000", "Trade Payables", "Current Liabilities", -238000, -221000),
        ("2010", "Accruals", "Current Liabilities", -61000, -58000),
        ("2020", "VAT Payable", "Current Liabilities", -44500, -41000),
        ("2100", "End of Service Benefits", "Non-Current Liabilities", -132000, -128500),
        ("3000", "Share Capital", "Equity", -300000, -300000),
    ]
    re_ = -350_000.0
    other_cm = sum(b[3] for b in bs) + re_
    other_pm = sum(b[4] for b in bs) + re_
    cash_cm = -(other_cm + pl_ytd)
    cash_pm = -(other_pm + pl_ytd_less_cm)
    for code, name, line, cm, pm in bs:
        rows.append([code, name, line, "", cm, pm, 0, cm, 0])
    rows.append(["3100", "Retained Earnings", "Equity", "", re_, re_, 0, re_, 0])
    rows.append(["1100", "Cash at Bank", "Cash", "", round(cash_cm, 2), round(cash_pm, 2), 0, round(cash_cm, 2), 0])

    wb = _book({"Company": "Sample Restaurant LLC", "Period": "Sep 2026", "Currency": "AED"}, rows)
    out = ROOT / "data" / "inbox" / "Sample_Restaurant_Sep2026_TB.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


if __name__ == "__main__":
    print(make_template())
    print(make_sample())
