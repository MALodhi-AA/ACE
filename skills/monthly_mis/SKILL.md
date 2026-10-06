# Monthly MIS Skill — v1.0

Owner: MA · Employee: ACE · Authority: read-only

## Objective

Review the month's financial performance from the trial balance and produce a
management information (MIS) report that a Finance Manager can review, correct
and send to management. The employee prepares; a human approves.

## Input

One Excel workbook in the inbox, prepared from the standard template
(`templates/MIS_Input_Template.xlsx`):

- Sheet **Info**: Company, Period (e.g. Sep 2026), Currency.
- Sheet **TB**: one row per account (per branch where branches are tracked).
  Columns: Account Code, Account Name, MIS Line, Branch, Current Month,
  Previous Month, Budget, YTD, YTD Prior Year.
- Sign convention: **Debit positive, Credit negative** (as exported from Tally).
- P&L accounts: Current Month / Previous Month / Budget are the month's
  movement; YTD and YTD Prior Year are year-to-date movements.
- Balance sheet accounts: Current Month and YTD hold the closing balance at
  the period end; Previous Month holds the closing balance at the previous
  month end. Equity excludes the current year's profit.

## Procedure

1. **Validate the trial balance.** Balance sheet closing balances plus P&L YTD
   must net to zero (within tolerance), for both the current and previous month
   end. Any account without an MIS line is an exception and is excluded from
   the P&L.
2. **Build the P&L** for current month, previous month, budget, YTD and prior
   year YTD: Revenue → Gross Profit → EBITDA → EBIT → Profit before tax → Net
   profit.
3. **Build the Balance Sheet summary** at current and previous month end, and
   confirm it balances after including current year profit.
4. **Compare** current month vs previous month, current month vs budget, and
   YTD vs prior year YTD, in AED and %.
5. **Calculate KPIs** as % of revenue: Gross Profit %, Food Cost %, Payroll %,
   Rent %, EBITDA %, Net Profit %. Flag any KPI that moves by more than the
   threshold in percentage points.
6. **Identify abnormal GL movements**: P&L accounts whose month-on-month change
   exceeds both the AED and % thresholds, plus accounts that appeared or
   disappeared.
7. **Review branch performance**: revenue, gross profit and EBITDA by branch,
   with change vs previous month and vs budget. Flag revenue declines above
   threshold.
8. **Identify accounting exceptions**: TB out of balance, unmapped accounts,
   suspense/clearing balances, intercompany balances to confirm, revenue with
   debit balances, expenses with credit balances, negative cash, assets with
   credit balances and liabilities with debit balances.
9. **Prepare management commentary** using only the computed figures.
10. **Generate the MIS report** (Excel) and post a short summary in chat.

## Commentary rules

- Lead with the result: revenue, gross profit %, EBITDA and EBITDA %.
- Then the three to five most important movements, each with the AED amount,
  the % or percentage-point change and the likely driver *if the data shows it*.
- Then exceptions that need a human to act, most serious first.
- Never invent a cause. If the data does not explain a movement, say
  "requires explanation from the accounts team".
- Never restate a figure differently from the calculated figures provided.
- Write for a business owner: short sentences, AED with thousands separators,
  percentages to one decimal place.
- Close with "Prepared by ACE, Accountability's Chief Examiner (Monthly MIS Skill v1.0) — for review
  by Finance before distribution."

## Known limitations (v1.0)

- Input is an Excel export; Tally is not connected yet.
- No cash-flow statement; no prior-year balance sheet comparison.
- Budget for balance sheet lines is ignored.
