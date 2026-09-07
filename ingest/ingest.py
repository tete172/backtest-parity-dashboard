"""fxbot.log を DB に取り込む。

- バイトオフセットを state ファイルに記録し、追記分だけを処理(tail 相当・冪等)。
- signal_events は line_hash(raw 行の SHA1)で重複排除。
- order(EXECUTED) / exit(CLOSED) から trades テーブルを導出・更新する。

使い方:
    python -m ingest.ingest                # 1 回だけ取り込む
    python -m ingest.ingest --follow       # 30 秒間隔で追記を取り込み続ける
    python -m ingest.ingest --path x.log   # 対象ログを指定
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cache import clear as clear_cache
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import AppMeta, BotHeartbeat, EquitySnapshot, SignalEvent, Trade

from .parser import LogEvent, parse_line


# --------------------------------------------------------------------------- #
# オフセット管理
# --------------------------------------------------------------------------- #
def _load_state(state_path: str) -> dict:
    p = Path(state_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state_path: str, state: dict) -> None:
    Path(state_path).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _state_key(log_path: str) -> str:
    return str(Path(log_path).resolve())


# --------------------------------------------------------------------------- #
# イベント適用
# --------------------------------------------------------------------------- #
def _apply_cycle(session: Session, ev: LogEvent) -> bool:
    cid = ev.data["cycle_id"]
    exists = session.scalar(
        select(BotHeartbeat.id).where(BotHeartbeat.cycle_id == cid)
    )
    if exists:
        return False
    session.add(
        BotHeartbeat(
            ts=ev.ts, cycle_id=cid, pairs_checked=ev.data.get("pairs_checked", 0)
        )
    )
    return True


def _apply_account(session: Session, ev: LogEvent) -> bool:
    exists = session.scalar(
        select(EquitySnapshot.id).where(EquitySnapshot.ts == ev.ts)
    )
    if exists:
        return False
    d = ev.data
    session.add(
        EquitySnapshot(
            ts=ev.ts,
            balance_jpy=d.get("balance_jpy") or 0.0,
            equity_jpy=d.get("equity_jpy") or 0.0,
            margin_used_jpy=d.get("margin_used_jpy") or 0.0,
            margin_ratio=d.get("margin_ratio"),
            open_positions=d.get("open_positions") or 0,
        )
    )
    return True


def _apply_signal_event(session: Session, ev: LogEvent) -> bool:
    exists = session.scalar(
        select(SignalEvent.id).where(SignalEvent.line_hash == ev.line_hash)
    )
    if exists:
        return False
    d = ev.data
    session.add(
        SignalEvent(
            ts=ev.ts,
            pair=d["pair"],
            strategy=d["strategy"],
            side=d.get("side"),
            event_type=ev.kind,
            result=d.get("result"),
            position_id=d.get("position_id"),
            price=d.get("price"),
            lot=d.get("lot"),
            pnl_jpy=d.get("pnl_jpy"),
            risk_pct=d.get("risk_pct"),
            reason=d.get("reason"),
            detail=d.get("detail"),
            line_hash=ev.line_hash,
        )
    )
    _update_trade(session, ev)
    return True


def _update_trade(session: Session, ev: LogEvent) -> None:
    """order(EXECUTED) で建て、exit(CLOSED) で閉じる。"""
    d = ev.data
    pos_id = d.get("position_id")
    if not pos_id:
        return

    if ev.kind == "order" and d.get("result") == "EXECUTED":
        exists = session.scalar(
            select(Trade.id).where(Trade.position_id == pos_id)
        )
        if exists:
            return
        session.add(
            Trade(
                position_id=pos_id,
                pair=d["pair"],
                strategy=d["strategy"],
                side=d.get("side") or "LONG",
                entry_time=ev.ts,
                entry_price=d.get("price") or 0.0,
                lot=d.get("lot") or 0.0,
                risk_pct=d.get("risk_pct"),
                status="open",
            )
        )
    elif ev.kind == "exit" and d.get("result") == "CLOSED":
        trade = session.scalar(select(Trade).where(Trade.position_id == pos_id))
        if trade is None:
            # entry ログを取りこぼしている場合でも決済だけは残す
            trade = Trade(
                position_id=pos_id,
                pair=d["pair"],
                strategy=d["strategy"],
                side=d.get("side") or "LONG",
                entry_time=ev.ts,
                entry_price=d.get("price") or 0.0,
                lot=d.get("lot") or 0.0,
                risk_pct=d.get("risk_pct"),
                status="open",
            )
            session.add(trade)
        trade.exit_time = ev.ts
        trade.exit_price = d.get("price")
        trade.pnl_jpy = d.get("pnl_jpy")
        trade.exit_reason = d.get("reason")
        trade.status = "closed"


_APPLY = {
    "cycle": _apply_cycle,
    "account": _apply_account,
    "signal": _apply_signal_event,
    "order": _apply_signal_event,
    "exit": _apply_signal_event,
}


# --------------------------------------------------------------------------- #
# 取り込み本体
# --------------------------------------------------------------------------- #
def ingest_once(log_path: str, state_path: str) -> dict[str, int]:
    if not os.path.exists(log_path):
        raise FileNotFoundError(log_path)

    state = _load_state(state_path)
    key = _state_key(log_path)
    entry = state.get(key, {})
    offset = int(entry.get("offset", 0))
    size = os.path.getsize(log_path)
    if size < offset:  # ログローテーション検知 → 先頭から
        offset = 0

    counts = {"cycle": 0, "account": 0, "signal_event": 0, "skipped": 0, "lines": 0}
    session = SessionLocal()
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            for line in fh:
                counts["lines"] += 1
                ev = parse_line(line)
                if ev is None:
                    counts["skipped"] += 1
                    continue
                applied = _APPLY[ev.kind](session, ev)
                if not applied:
                    counts["skipped"] += 1
                    continue
                # 直後のイベント(例: 同一 position_id の exit)が、まだ未コミットの
                # 行を検索できるように flush して可視化する。
                session.flush()
                counts["signal_event" if ev.kind in ("signal", "order", "exit") else ev.kind] += 1
            new_offset = fh.tell()
        # 実ログを取り込んだら demo 表示を解除する
        if counts["lines"] > 0:
            session.merge(AppMeta(key="data_mode", value="live (実ログ取り込み済み)"))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    state[key] = {"offset": new_offset, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _save_state(state_path, state)
    clear_cache()
    return counts


def main() -> None:
    settings = get_settings()
    ap = argparse.ArgumentParser(description="fxbot.log を DB に取り込む")
    ap.add_argument("--path", default=settings.fxbot_log_path)
    ap.add_argument("--state", default=settings.ingest_state_path)
    ap.add_argument("--follow", action="store_true", help="30 秒間隔で追記を取り込み続ける")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()

    init_db()
    if args.follow:
        print(f"follow モード: {args.path} を {args.interval}s 間隔で監視")
        while True:
            try:
                c = ingest_once(args.path, args.state)
                print(time.strftime("%H:%M:%S"), c)
            except FileNotFoundError:
                print("ログ未検出、待機中...")
            time.sleep(args.interval)
    else:
        print(ingest_once(args.path, args.state))


if __name__ == "__main__":
    main()
