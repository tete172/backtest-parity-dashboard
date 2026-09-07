"""ログパーサの単体テスト(DB 非依存)。"""

from __future__ import annotations

from ingest.parser import parse_line, parse_lines


def test_cycle_line():
    ev = parse_line(
        "2026-09-01 09:01:03 INFO  [cycle]   メインチェック開始 cycle=20260901-0901 pairs=12"
    )
    assert ev is not None
    assert ev.kind == "cycle"
    assert ev.data["cycle_id"] == "20260901-0901"
    assert ev.data["pairs_checked"] == 12
    assert ev.ts.year == 2026 and ev.ts.hour == 9


def test_account_line_japanese_keys():
    ev = parse_line(
        "2026-09-01 09:01:04 INFO  [account] 有効証拠金=302923 残高=310450 "
        "必要証拠金=41200 証拠金維持率=735.2 建玉数=2"
    )
    assert ev is not None and ev.kind == "account"
    assert ev.data["equity_jpy"] == 302923
    assert ev.data["balance_jpy"] == 310450
    assert ev.data["margin_used_jpy"] == 41200
    assert ev.data["margin_ratio"] == 735.2
    assert ev.data["open_positions"] == 2


def test_order_executed_line():
    ev = parse_line(
        "2026-09-01 09:01:08 INFO  [order]   pair=USD_JPY strategy=SB side=LONG "
        "result=EXECUTED positionId=9474212 price=147.512 lot=8000"
    )
    assert ev is not None and ev.kind == "order"
    assert ev.data["result"] == "EXECUTED"
    assert ev.data["position_id"] == "9474212"
    assert ev.data["price"] == 147.512
    assert ev.data["lot"] == 8000
    assert ev.data["side"] == "LONG"


def test_order_margin_insufficient_with_quoted_detail():
    ev = parse_line(
        '2026-09-01 09:01:10 WARN  [order]   pair=GBP_USD strategy=MACD side=SHORT '
        'result=MARGIN_INSUFFICIENT detail="Trading margin is insufficient"'
    )
    assert ev is not None
    assert ev.data["result"] == "MARGIN_INSUFFICIENT"
    assert ev.data["detail"] == "Trading margin is insufficient"
    assert ev.data["side"] == "SHORT"


def test_exit_line():
    ev = parse_line(
        "2026-09-01 12:33:22 INFO  [exit]    pair=USD_JPY strategy=SB side=LONG "
        "result=CLOSED positionId=9474212 price=148.031 pnl_jpy=4152 reason=TP"
    )
    assert ev is not None and ev.kind == "exit"
    assert ev.data["result"] == "CLOSED"
    assert ev.data["pnl_jpy"] == 4152
    assert ev.data["reason"] == "TP"


def test_non_matching_lines_return_none():
    assert parse_line("") is None
    assert parse_line("2026-09-02 09:01:04 DEBUG [misc] シグナルなし") is None
    assert parse_line("ただのテキスト行") is None


def test_line_hash_is_stable_and_unique():
    a = parse_line(
        "2026-09-01 09:01:08 INFO  [order]   pair=USD_JPY strategy=SB side=LONG "
        "result=EXECUTED positionId=9474212 price=147.512 lot=8000"
    )
    b = parse_line(
        "2026-09-01 09:01:08 INFO  [order]   pair=USD_JPY strategy=SB side=LONG "
        "result=EXECUTED positionId=9474212 price=147.512 lot=8000"
    )
    c = parse_line(
        "2026-09-01 09:01:12 INFO  [order]   pair=GBP_USD strategy=MACD side=SHORT "
        "result=EXECUTED positionId=9474213 price=1.34210 lot=6000"
    )
    assert a.line_hash == b.line_hash
    assert a.line_hash != c.line_hash


def test_parse_lines_filters():
    lines = [
        "2026-09-01 09:01:03 INFO  [cycle]   メインチェック開始 cycle=20260901-0901 pairs=12",
        "ゴミ行",
        "2026-09-01 09:01:04 INFO  [account] 有効証拠金=302923 残高=310450 建玉数=0",
    ]
    events = parse_lines(lines)
    assert [e.kind for e in events] == ["cycle", "account"]
