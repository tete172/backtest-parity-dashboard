"""fxbot.log の 1 行を構造化イベントに変換する(純粋関数・DB 非依存)。

対象フォーマット(監視対象ボットの自前ロガー出力を想定):

    2026-09-01 09:01:03 INFO  [cycle]   メインチェック開始 cycle=20260901-0901 pairs=12
    2026-09-01 09:01:04 INFO  [account] 有効証拠金=302923 残高=310450 必要証拠金=41200 証拠金維持率=735.2 建玉数=2
    2026-09-01 09:01:07 INFO  [signal]  pair=USD_JPY strategy=SB side=LONG action=ENTRY risk_pct=0.02 detail=entry_signal
    2026-09-01 09:01:08 INFO  [order]   pair=USD_JPY strategy=SB side=LONG result=EXECUTED positionId=1000001 price=147.512 lot=8000
    2026-09-01 09:01:09 WARN  [order]   pair=GBP_USD strategy=MACD side=SHORT result=MARGIN_INSUFFICIENT detail="Trading margin is insufficient"
    2026-09-01 12:33:20 INFO  [exit]    pair=USD_JPY strategy=SB side=LONG result=CLOSED positionId=1000001 price=148.031 pnl_jpy=4152 reason=TP

実運用のログ書式が変わった場合はこのモジュールの正規表現だけ直せばよい設計。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s+"
    r"(?P<level>[A-Z]+)\s+"
    r"\[(?P<tag>[a-zA-Z_]+)\]\s+"
    r"(?P<body>.*\S)\s*$"
)

# key=value / key="quoted value" を拾う
_KV_RE = re.compile(r'([a-zA-Z_]+)=("[^"]*"|\'[^\']*\'|[^\s]+)')

_SIDE_MAP = {
    "LONG": "LONG", "BUY": "LONG", "L": "LONG",
    "SHORT": "SHORT", "SELL": "SHORT", "S": "SHORT",
}

# ログ内の日本語キー -> 正規化キー
_ACCOUNT_KEYS = {
    "有効証拠金": "equity_jpy",
    "残高": "balance_jpy",
    "必要証拠金": "margin_used_jpy",
    "証拠金維持率": "margin_ratio",
    "建玉数": "open_positions",
}
_ACCOUNT_JP_RE = re.compile(
    r"(有効証拠金|残高|必要証拠金|証拠金維持率|建玉数)=(-?[0-9.]+)"
)
_CYCLE_RE = re.compile(r"cycle=([0-9A-Za-z\-]+).*?pairs=(\d+)", re.DOTALL)


@dataclass
class LogEvent:
    kind: str  # cycle / account / signal / order / exit
    ts: datetime
    raw: str
    data: dict[str, Any] = field(default_factory=dict)
    line_hash: str = ""

    def __post_init__(self) -> None:
        if not self.line_hash:
            self.line_hash = hashlib.sha1(self.raw.strip().encode("utf-8")).hexdigest()


def _parse_ts(text: str) -> datetime:
    text = text.replace("T", " ")
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _kv(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, val in _KV_RE.findall(body):
        out[key] = val.strip("\"'")
    return out


def _num(val: str | None) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def parse_line(line: str) -> LogEvent | None:
    """1 行を LogEvent にする。対象外の行は None。"""
    line = line.rstrip("\n")
    m = _LINE_RE.match(line)
    if not m:
        return None

    ts = _parse_ts(m.group("ts"))
    tag = m.group("tag").lower()
    body = m.group("body")

    if tag == "cycle":
        cm = _CYCLE_RE.search(body)
        if not cm:
            return None
        return LogEvent(
            kind="cycle",
            ts=ts,
            raw=line,
            data={"cycle_id": cm.group(1), "pairs_checked": int(cm.group(2))},
        )

    if tag == "account":
        data: dict[str, Any] = {}
        for jp, num in _ACCOUNT_JP_RE.findall(body):
            data[_ACCOUNT_KEYS[jp]] = _num(num)
        if "open_positions" in data and data["open_positions"] is not None:
            data["open_positions"] = int(data["open_positions"])
        if not data:
            return None
        return LogEvent(kind="account", ts=ts, raw=line, data=data)

    if tag in ("signal", "order", "exit"):
        kv = _kv(body)
        side = _SIDE_MAP.get((kv.get("side") or "").upper())
        data = {
            "pair": kv.get("pair"),
            "strategy": kv.get("strategy"),
            "side": side,
            "action": kv.get("action"),
            "result": (kv.get("result") or "").upper() or None,
            "position_id": kv.get("positionId") or kv.get("position_id"),
            "price": _num(kv.get("price")),
            "lot": _num(kv.get("lot")),
            "pnl_jpy": _num(kv.get("pnl_jpy")),
            "risk_pct": _num(kv.get("risk_pct")),
            "reason": kv.get("reason"),
            "detail": kv.get("detail"),
        }
        if not data["pair"] or not data["strategy"]:
            return None
        return LogEvent(kind=tag, ts=ts, raw=line, data=data)

    return None


def parse_lines(lines) -> list[LogEvent]:
    events: list[LogEvent] = []
    for line in lines:
        ev = parse_line(line)
        if ev is not None:
            events.append(ev)
    return events
