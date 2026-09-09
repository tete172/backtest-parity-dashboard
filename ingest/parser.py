"""fxbot.log の 1 行を構造化イベントに変換する(純粋関数・DB 非依存)。

対象は監視対象ボットの自前ロガー出力(実書式・2026-09 の実ログで確認):

    ==============================================================        ← 区切り(無視)
    [2026-06-07 05:01:02] メインチェック開始（3手法）
    [2026-06-07 05:01:02] 有効証拠金: 109,097円
    [2026-06-07 05:01:03] 現在ポジション: 0 / 6
    [2026-06-07 05:01:03]   [USD_JPY] シグナルなし。
    [2026-06-09 15:01:02]   [AUD_JPY] ★ [SB] SELL シグナル！
    [2026-06-09 15:01:03]            price=112.82750  SL=113.25256  TP=112.32036
    [2026-06-09 15:01:03]            units=10,000  risk=6,955円
    [2026-06-09 15:01:06]   [AUD_JPY] 約定確認 positionId=8882904
    [2026-06-08 14:01:13]   [CAD_JPY] 発注失敗: {'status': 1, 'messages': [{'message_string': 'Trading margin is insufficient.'}], ...}
    [2026-08-19 12:48:23] [決済検知] NZD_JPY pid=9325460
    [2026-07-16 11:01:08]   [CAD_JPY] 複数positionId検出: 2件 全件にエグジット登録

タイムスタンプは EC2 のローカル時刻(= UTC。ログ末尾時刻とファイル mtime の突合で確認)。
`約定確認` / `発注失敗` / `price=` / `units=` の行には手法名が無いため、
ingest 側で直前の `★ [手法]` シグナルから手法・方向・価格を引き継ぐ。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s?(.*)$")

_SIG = re.compile(r"\[(\w+)\]\s*★\s*\[(\w+)\]\s*(BUY|SELL)\s*シグナル")
_PARAMS = re.compile(r"price=([\d.]+)\s+SL=([\d.]+)\s+TP=([\d.]+)")
_LOT = re.compile(r"units=([\d,]+)\s+risk=([\d,]+)\s*円")
_OK = re.compile(r"\[(\w+)\]\s*約定確認\s*positionId=(\d+)")
_FAIL = re.compile(r"\[(\w+)\]\s*発注失敗:")
_EXIT = re.compile(r"\[決済検知\]\s*(\w+)\s*pid=(\d+)")
_MULTI = re.compile(r"\[(\w+)\]\s*複数positionId検出:\s*(\d+)")
_BAL = re.compile(r"有効証拠金:\s*([\d,]+)")
_POS = re.compile(r"現在ポジション:\s*(\d+)\s*/\s*(\d+)")

_SIDE = {"BUY": "LONG", "SELL": "SHORT"}
_STRAT = {"DM": "DM_PSAR"}  # ログ表記 → 正規化


def _norm_strat(s: str) -> str:
    return _STRAT.get(s, s)


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


@dataclass
class LogEvent:
    # cycle / balance / positions / signal / entry_params / lot /
    # order_ok / order_fail / exit / multi_position
    kind: str
    ts: datetime
    raw: str
    data: dict[str, Any] = field(default_factory=dict)
    line_hash: str = ""

    def __post_init__(self) -> None:
        if not self.line_hash:
            self.line_hash = hashlib.sha1(self.raw.strip().encode("utf-8")).hexdigest()


def _ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def parse_line(line: str) -> LogEvent | None:
    """1 行を LogEvent にする。対象外(区切り線・空行・シグナルなし 等)は None。"""
    raw = line.rstrip("\n")
    m = _LINE.match(raw)
    if not m:
        return None
    ts = _ts(m.group(1))
    body = m.group(2)

    if "メインチェック開始" in body:
        return LogEvent("cycle", ts, raw, {})

    mm = _BAL.search(body)
    if mm and "有効証拠金" in body:
        return LogEvent("balance", ts, raw, {"balance_jpy": _num(mm.group(1))})

    mm = _POS.search(body)
    if mm and "現在ポジション" in body:
        return LogEvent(
            "positions", ts, raw,
            {"open_positions": int(mm.group(1)), "max_positions": int(mm.group(2))},
        )

    mm = _SIG.search(body)
    if mm:
        return LogEvent(
            "signal", ts, raw,
            {"pair": mm.group(1), "strategy": _norm_strat(mm.group(2)), "side": _SIDE.get(mm.group(3))},
        )

    if "price=" in body:
        mm = _PARAMS.search(body)
        if mm:
            return LogEvent(
                "entry_params", ts, raw,
                {"price": _num(mm.group(1)), "sl": _num(mm.group(2)), "tp": _num(mm.group(3))},
            )

    if "units=" in body:
        mm = _LOT.search(body)
        if mm:
            return LogEvent(
                "lot", ts, raw,
                {"units": _num(mm.group(1)), "risk_jpy": _num(mm.group(2))},
            )

    mm = _OK.search(body)
    if mm:
        return LogEvent(
            "order_ok", ts, raw,
            {"pair": mm.group(1), "position_id": mm.group(2)},
        )

    mm = _FAIL.search(body)
    if mm:
        reason = "MARGIN_INSUFFICIENT" if "insufficient" in body.lower() else "FAILED"
        return LogEvent("order_fail", ts, raw, {"pair": mm.group(1), "reason": reason})

    mm = _EXIT.search(body)
    if mm:
        return LogEvent(
            "exit", ts, raw,
            {"pair": mm.group(1), "position_id": mm.group(2)},
        )

    mm = _MULTI.search(body)
    if mm:
        return LogEvent(
            "multi_position", ts, raw,
            {"pair": mm.group(1), "count": int(mm.group(2))},
        )

    return None


def parse_lines(lines) -> list[LogEvent]:
    return [ev for ev in (parse_line(x) for x in lines) if ev is not None]
