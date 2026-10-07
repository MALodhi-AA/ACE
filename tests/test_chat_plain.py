from integrations.synology_chat.client import plain


def test_plain_strips_markdown():
    src = "## EBITDA\n**Earnings** before __interest__\n* one\n  * two\nuse `mis`\n5 * 3 = 15"
    assert plain(src) == "EBITDA\nEarnings before interest\n- one\n  - two\nuse mis\n5 * 3 = 15"
