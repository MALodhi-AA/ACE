"""v0.8.0: Tally read-only client."""
from datetime import date

import httpx
import pytest

from integrations.tally import probe
from integrations.tally.client import NotReadOnly, Tally, amount, check_read_only, dr_cr, parse_tally_date

COMPANIES = b"""<ENVELOPE><BODY><DATA><COLLECTION>
<COMPANY NAME="Mara Trading LLC"><NAME>Mara Trading LLC</NAME><BOOKSFROM>20250101</BOOKSFROM></COMPANY>
<COMPANY NAME="Volt Electric &#4;FZE"><BOOKSFROM>20240401</BOOKSFROM></COMPANY>
</COLLECTION></DATA></BODY></ENVELOPE>"""
LEDGERS = """<ENVELOPE><BODY><DATA><COLLECTION>
<LEDGER NAME="Cash"><PARENT>Cash-in-Hand</PARENT><OPENINGBALANCE>-1,000.00</OPENINGBALANCE><CLOSINGBALANCE>-2,500.00</CLOSINGBALANCE></LEDGER>
<LEDGER NAME="Capital"><PARENT>Capital Account</PARENT><OPENINGBALANCE>1000</OPENINGBALANCE><CLOSINGBALANCE>2500.00</CLOSINGBALANCE></LEDGER>
</COLLECTION></DATA></BODY></ENVELOPE>""".encode("utf-16")       # Tally may answer in UTF-16
VOUCHERS = b"""<ENVELOPE><BODY><DATA><COLLECTION>
<VOUCHER><DATE>20260930</DATE></VOUCHER><VOUCHER><DATE>20261003</DATE></VOUCHER>
</COLLECTION></DATA></BODY></ENVELOPE>"""


def fake_tally(sent):
    def handler(request):
        body = request.content.decode()
        sent.append(body)
        if "<TYPE>Company</TYPE>" in body:
            return httpx.Response(200, content=COMPANIES)
        if "<TYPE>Ledger</TYPE>" in body:
            return httpx.Response(200, content=LEDGERS)
        if "<TYPE>Voucher</TYPE>" in body:
            return httpx.Response(200, content=VOUCHERS)
        return httpx.Response(200, content=b"<ENVELOPE><BODY><DATA><DSPACCNAME/></DATA></BODY></ENVELOPE>")
    return Tally(url="http://tally:9000", transport=httpx.MockTransport(handler))


def test_reads_companies_ledgers_and_last_entry():
    sent = []
    t = fake_tally(sent)
    cs = t.companies()
    assert [c["name"] for c in cs] == ["Mara Trading LLC", "Volt Electric FZE"]
    assert cs[0]["books_from"] == date(2025, 1, 1)
    ls = t.ledgers("Mara Trading LLC", date(2026, 1, 1), date(2026, 9, 30))
    assert [(l.name, l.parent, l.closing) for l in ls] == [("Cash", "Cash-in-Hand", -2500.0), ("Capital", "Capital Account", 2500.0)]
    assert "<SVCURRENTCOMPANY>Mara Trading LLC</SVCURRENTCOMPANY>" in sent[-1] and '<SVTODATE TYPE="Date">30-Sep-2026</SVTODATE>' in sent[-1]
    assert t.last_voucher_date("Mara Trading LLC") == date(2026, 10, 3)
    assert all("<TALLYREQUEST>Export</TALLYREQUEST>" in s for s in sent)


@pytest.mark.parametrize("xml", [
    "<ENVELOPE><HEADER><TALLYREQUEST>Import</TALLYREQUEST></HEADER><BODY/></ENVELOPE>",
    "<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST></HEADER><BODY><IMPORTDATA/></BODY></ENVELOPE>",
    "<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST></HEADER><BODY><DESC><TDL><TDLMESSAGE>"
    "<FUNCTION NAME='x'/></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>",
    "<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST></HEADER><BODY><DESC><TDL><TDLMESSAGE>"
    "<COLLECTION NAME='x'><ACTION>Delete</ACTION></COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>",
    "<ENVELOPE><HEADER><TALLYREQUEST>Execute</TALLYREQUEST></HEADER></ENVELOPE>",
    "not xml",
])
def test_anything_that_could_write_is_refused_before_sending(xml):
    sent = []
    t = fake_tally(sent)
    with pytest.raises(NotReadOnly):
        t.request(xml)
    assert sent == []


def test_names_with_write_words_are_still_fine():
    check_read_only(Tally._envelope("Data", "Trial Balance", company="Create & Alter Trading LLC"))


def test_amount_helpers():
    assert amount("-1,234.50") == -1234.5 and amount("1234.50 Dr") == -1234.5 and amount("") == 0
    assert amount("$ 100.00 @ AED 3.67/$ = -367.00") == -367.0
    assert dr_cr(-1234.5) == "1,234.50 Dr" and dr_cr(10) == "10.00 Cr"
    assert parse_tally_date("1-Apr-2026") == date(2026, 4, 1)


def test_probe_lists_companies_and_one_company(capsys):
    t = fake_tally([])
    assert probe.main([], tally=t) == 0
    out = capsys.readouterr().out
    assert "2 companies open" in out and "Mara Trading LLC" in out
    assert probe.main(["--company", "mara"], tally=t) == 0
    out = capsys.readouterr().out
    assert "Ledgers: 2 in 2 groups" in out and "difference 0.00" in out and "Last entry" in out
    assert "Cash" not in out.split("Company:")[1]                # no ledger names without --show


def test_probe_reports_connection_problem(capsys):
    def down(request):
        raise httpx.ConnectError("refused")
    t = Tally(url="http://tally:9000", transport=httpx.MockTransport(down))
    assert probe.main([], tally=t) == 1
    assert "Connectivity set to Server/Both" in capsys.readouterr().out


TB = b"""<ENVELOPE><DSPACCNAME><DSPDISPNAME>Capital Account</DSPDISPNAME></DSPACCNAME>
<DSPACCINFO><DSPCLDRAMT><DSPCLDRAMTA></DSPCLDRAMTA></DSPCLDRAMT><DSPCLCRAMT><DSPCLCRAMTA>5000.00</DSPCLCRAMTA></DSPCLCRAMT></DSPACCINFO>
<DSPACCNAME><DSPDISPNAME>Current Assets</DSPDISPNAME></DSPACCNAME>
<DSPACCINFO><DSPCLDRAMT><DSPCLDRAMTA>-5000.00</DSPCLDRAMTA></DSPCLDRAMT><DSPCLCRAMT><DSPCLCRAMTA></DSPCLCRAMTA></DSPCLCRAMT></DSPACCINFO>
</ENVELOPE>"""


def test_period_is_typed_and_also_written_as_filter():
    sent = []

    def handler(request):
        sent.append(request.content.decode())
        return httpx.Response(200, content=VOUCHERS if "<TYPE>Voucher</TYPE>" in sent[-1] else TB)
    t = Tally(url="http://tally:9000", transport=httpx.MockTransport(handler))
    t.voucher_dates("Mara", date(2024, 1, 1), date(2024, 12, 31))
    body = sent[-1]
    assert '<SVFROMDATE TYPE="Date">1-Jan-2024</SVFROMDATE>' in body
    assert "<FILTERS>ACEFrom</FILTERS>" in body and 'NAME="ACEFrom">$Date &gt;= $$Date:"1-Jan-2024"' in body
    check_read_only(body)
    assert t.trial_balance("Mara") == [("Capital Account", 5000.0), ("Current Assets", -5000.0)]


def test_probe_date_check(capsys):
    def handler(request):
        b = request.content.decode()
        if "<TYPE>Company</TYPE>" in b:
            return httpx.Response(200, content=COMPANIES)
        if "<TYPE>Ledger</TYPE>" in b:
            return httpx.Response(200, content=LEDGERS)
        if "<TYPE>Voucher</TYPE>" in b:
            return httpx.Response(200, content=VOUCHERS)
        return httpx.Response(200, content=TB)
    t = Tally(url="http://tally:9000", transport=httpx.MockTransport(handler))
    assert probe.main(["--company", "mara", "--dates"], tally=t) == 0
    out = capsys.readouterr().out
    assert "Vouchers, period as filter: 2 vouchers, 30 Sep 2026 to 03 Oct 2026" in out
    assert "Trial Balance to 31 Dec 2024: debits 5,000.00 / credits 5,000.00" in out
