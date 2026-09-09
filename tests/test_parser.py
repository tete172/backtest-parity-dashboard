"""ログパーサの単体テスト(実書式・DB 非依存)。"""

from __future__ import annotations

from ingest.parser import parse_line, parse_lines


def test_cycle_line():
    ev = parse_line("[2026-06-07 05:01:02] メインチェック開始（3手法）")
    assert ev is not None and ev.kind == "cycle"
    assert ev.ts.year == 2026 and ev.ts.hour == 5 and str(ev.ts.tzinfo) == "UTC"


def test_balance_line():
    ev = parse_line("[2026-06-07 05:01:02] 有効証拠金: 109,097円")
    assert ev.kind == "balance" and ev.data["balance_jpy"] == 109097


def test_positions_line():
    ev = parse_line("[2026-06-07 05:01:03] 現在ポジション: 4 / 6")
    assert ev.kind == "positions"
    assert ev.data["open_positions"] == 4 and ev.data["max_positions"] == 6


def test_signal_line():
    ev = parse_line("[2026-06-09 15:01:02]   [AUD_JPY] ★ [SB] SELL シグナル！")
    assert ev.kind == "signal"
    assert ev.data == {"pair": "AUD_JPY", "strategy": "SB", "side": "SHORT"}


def test_signal_dm_normalized_and_buy_long():
    ev = parse_line("[2026-06-08 14:01:11]   [CAD_JPY] ★ [DM] BUY シグナル！")
    assert ev.data["strategy"] == "DM_PSAR" and ev.data["side"] == "LONG"


def test_entry_params_and_lot():
    p = parse_line("[2026-06-09 15:01:03]            price=112.82750  SL=113.25256  TP=112.32036")
    assert p.kind == "entry_params" and p.data["price"] == 112.8275 and p.data["sl"] == 113.25256
    lot = parse_line("[2026-06-09 15:01:03]            units=10,000  risk=6,955円")
    assert lot.kind == "lot" and lot.data["units"] == 10000 and lot.data["risk_jpy"] == 6955


def test_order_ok_line():
    ev = parse_line("[2026-06-09 15:01:06]   [AUD_JPY] 約定確認 positionId=8882904")
    assert ev.kind == "order_ok"
    assert ev.data == {"pair": "AUD_JPY", "position_id": "8882904"}


def test_order_fail_margin_insufficient():
    ev = parse_line(
        "[2026-06-08 14:01:13]   [CAD_JPY] 発注失敗: {'status': 1, 'messages': "
        "[{'message_code': 'ERR-201', 'message_string': 'Trading margin is insufficient.'}]}"
    )
    assert ev.kind == "order_fail"
    assert ev.data["pair"] == "CAD_JPY" and ev.data["reason"] == "MARGIN_INSUFFICIENT"


def test_order_fail_other():
    ev = parse_line("[2026-06-08 14:01:13]   [EUR_JPY] 発注失敗: {'status': 5, 'messages': []}")
    assert ev.kind == "order_fail" and ev.data["reason"] == "FAILED"


def test_exit_line():
    ev = parse_line("[2026-08-19 12:48:23] [決済検知] NZD_JPY pid=9325460")
    assert ev.kind == "exit"
    assert ev.data == {"pair": "NZD_JPY", "position_id": "9325460"}


def test_multi_position_line():
    ev = parse_line("[2026-07-16 11:01:08]   [CAD_JPY] 複数positionId検出: 2件 全件にエグジット登録")
    assert ev.kind == "multi_position"
    assert ev.data == {"pair": "CAD_JPY", "count": 2}


def test_non_matching_lines_return_none():
    assert parse_line("") is None
    assert parse_line("==============================================================") is None
    assert parse_line("[2026-06-07 05:01:03]   [USD_JPY] シグナルなし。") is None
    assert parse_line("[2026-06-09 16:01:05]   [AUD_JPY] ポジションあり。スキップ。") is None
    assert parse_line("稼働待機中... (Ctrl+C で停止)") is None
    assert parse_line("[2026-07-06 08:01:14]   [OCO決済発注] EUR_GBP BUY TP=0.85 SL=0.85") is None


def test_line_hash_stable_and_unique():
    a = parse_line("[2026-06-09 15:01:06]   [AUD_JPY] 約定確認 positionId=8882904")
    b = parse_line("[2026-06-09 15:01:06]   [AUD_JPY] 約定確認 positionId=8882904")
    c = parse_line("[2026-06-09 15:01:13]   [AUD_USD] 約定確認 positionId=8882906")
    assert a.line_hash == b.line_hash and a.line_hash != c.line_hash


def test_parse_lines_filters():
    lines = [
        "==============================================================",
        "[2026-06-07 05:01:02] メインチェック開始（4手法）",
        "[2026-06-07 05:01:02] 有効証拠金: 109,097円",
        "[2026-06-07 05:01:03]   [USD_JPY] シグナルなし。",
    ]
    assert [e.kind for e in parse_lines(lines)] == ["cycle", "balance"]
