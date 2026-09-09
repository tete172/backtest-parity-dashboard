"""fxbot.log を DB に取り込む。

- バイトオフセットを state ファイルに記録し、追記分だけを処理(tail 相当・冪等)。
- signal_events は line_hash(raw 行の SHA1)で重複排除。
- `約定確認` / `発注失敗` の行には手法名が無いため、直前の `★ [手法]` シグナルから
  手法・方向・エントリー価格・ロットを引き継ぐ(ステートマシン)。
- ログに決済価格・損益は残らない(GMO 側 OCO で閉じるため)。`[決済検知]` があれば
  その trade を closed にするが、exit_price / pnl_jpy は NULL のまま。

使い方:
    python -m ingest.ingest --path fxbot.log
    python -m ingest.ingest --path fxbot.log --follow --interval 60
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


def _cycle_id(ts) -> str:
    return ts.strftime("%Y%m%d-%H%M")


# --------------------------------------------------------------------------- #
# 取り込みステートマシン
# --------------------------------------------------------------------------- #
class Ingestor:
    def __init__(self, session: Session):
        self.s = session
        self.counts = {
            "lines": 0, "skipped": 0, "cycle": 0, "equity": 0,
            "signal": 0, "order_ok": 0, "order_fail": 0, "exit": 0, "double": 0,
        }
        # 直前のシグナル(手法名の無い後続行に引き継ぐ)。pair -> dict
        self.pending: dict[str, dict] = {}
        self.last_sig_pair: str | None = None
        # 現サイクルの口座情報
        self.cur_ts = None
        self.cur_balance: float | None = None
        self.cur_open: int | None = None
        self.equity_written: set = set()

    def _risk_pct(self, risk_jpy) -> float | None:
        """ログの `risk=N円` と直近の有効証拠金から実効リスク% を逆算。"""
        if risk_jpy and self.cur_balance:
            return round(risk_jpy / self.cur_balance, 5)
        return None

    # --- 冪等ヘルパ -------------------------------------------------------- #
    def _seen(self, line_hash: str) -> bool:
        return self.s.scalar(
            select(SignalEvent.id).where(SignalEvent.line_hash == line_hash)
        ) is not None

    def _add_signal_event(self, ev: LogEvent, event_type: str, **kw) -> None:
        if self._seen(ev.line_hash):
            return
        self.s.add(SignalEvent(
            ts=ev.ts, event_type=event_type, line_hash=ev.line_hash,
            pair=kw.get("pair", "-"), strategy=kw.get("strategy") or "-",
            side=kw.get("side"), result=kw.get("result"),
            position_id=kw.get("position_id"), price=kw.get("price"),
            lot=kw.get("lot"), risk_pct=kw.get("risk_pct"),
            reason=kw.get("reason"), detail=kw.get("detail"),
        ))
        self.s.flush()

    def _try_equity(self) -> None:
        if (self.cur_ts is not None and self.cur_balance is not None
                and self.cur_open is not None and self.cur_ts not in self.equity_written):
            exists = self.s.scalar(
                select(EquitySnapshot.id).where(EquitySnapshot.ts == self.cur_ts)
            )
            if not exists:
                self.s.add(EquitySnapshot(
                    ts=self.cur_ts, balance_jpy=self.cur_balance,
                    equity_jpy=self.cur_balance, margin_used_jpy=0.0,
                    margin_ratio=None, open_positions=self.cur_open,
                ))
                self.s.flush()
                self.counts["equity"] += 1
            self.equity_written.add(self.cur_ts)

    # --- イベント適用 --------------------------------------------------- #
    def apply(self, ev: LogEvent) -> None:
        k = ev.kind
        if k == "cycle":
            cid = _cycle_id(ev.ts)
            if not self.s.scalar(select(BotHeartbeat.id).where(BotHeartbeat.cycle_id == cid)):
                self.s.add(BotHeartbeat(ts=ev.ts, cycle_id=cid, pairs_checked=12))
                self.s.flush()
                self.counts["cycle"] += 1
            self.cur_ts, self.cur_balance, self.cur_open = ev.ts, None, None

        elif k == "balance":
            self.cur_balance = ev.data["balance_jpy"]
            self._try_equity()

        elif k == "positions":
            self.cur_open = ev.data["open_positions"]
            self._try_equity()

        elif k == "signal":
            d = ev.data
            self.pending[d["pair"]] = {
                "strategy": d["strategy"], "side": d["side"],
                "price": None, "units": None, "risk_jpy": None,
            }
            self.last_sig_pair = d["pair"]
            self._add_signal_event(
                ev, "signal", pair=d["pair"], strategy=d["strategy"], side=d["side"],
                detail="entry_signal",
            )
            self.counts["signal"] += 1

        elif k == "entry_params":
            if self.last_sig_pair in self.pending:
                self.pending[self.last_sig_pair]["price"] = ev.data["price"]

        elif k == "lot":
            if self.last_sig_pair in self.pending:
                self.pending[self.last_sig_pair]["units"] = ev.data["units"]
                self.pending[self.last_sig_pair]["risk_jpy"] = ev.data["risk_jpy"]

        elif k == "order_ok":
            pair = ev.data["pair"]
            pid = ev.data["position_id"]
            p = self.pending.get(pair, {})
            strat = p.get("strategy")
            rp = self._risk_pct(p.get("risk_jpy"))
            self._add_signal_event(
                ev, "order", pair=pair, strategy=strat, side=p.get("side"),
                result="EXECUTED", position_id=pid, price=p.get("price"),
                lot=p.get("units"), risk_pct=rp,
            )
            if not self.s.scalar(select(Trade.id).where(Trade.position_id == pid)):
                # ログに決済記録が無いため、同じペアの旧建玉が残っていたら
                # 新規エントリー時刻でクローズ扱いにする(ボットは 1 ペア 1 ポジション)。
                prev = self.s.scalar(
                    select(Trade).where(Trade.pair == pair, Trade.status == "open")
                )
                if prev is not None:
                    prev.exit_time = ev.ts
                    prev.status = "closed"
                    prev.exit_reason = "次エントリー時に推定クローズ(ログに決済記録なし)"
                self.s.add(Trade(
                    position_id=pid, pair=pair, strategy=strat or "SB",
                    side=p.get("side") or "LONG", entry_time=ev.ts,
                    entry_price=p.get("price") or 0.0, lot=p.get("units") or 0.0,
                    risk_pct=rp, status="open",
                ))
                self.s.flush()
            self.pending.pop(pair, None)
            self.counts["order_ok"] += 1

        elif k == "order_fail":
            pair = ev.data["pair"]
            p = self.pending.get(pair, {})
            self._add_signal_event(
                ev, "order", pair=pair, strategy=p.get("strategy"), side=p.get("side"),
                result=ev.data["reason"],
                reason="Trading margin is insufficient"
                if ev.data["reason"] == "MARGIN_INSUFFICIENT" else None,
            )
            self.pending.pop(pair, None)
            self.counts["order_fail"] += 1

        elif k == "exit":
            pid = ev.data["position_id"]
            tr = self.s.scalar(select(Trade).where(Trade.position_id == pid))
            if tr is None:
                tr = Trade(
                    position_id=pid, pair=ev.data["pair"], strategy="SB",
                    side="LONG", entry_time=ev.ts, entry_price=0.0, lot=0.0,
                    status="open",
                )
                self.s.add(tr)
            tr.exit_time = ev.ts
            tr.exit_reason = "決済検知(価格・損益はログに無し)"
            tr.status = "closed"
            self.s.flush()
            self._add_signal_event(
                ev, "exit", pair=ev.data["pair"], result="CLOSED", position_id=pid,
            )
            self.counts["exit"] += 1

        elif k == "multi_position":
            pair = ev.data["pair"]
            p = self.pending.get(pair, {})
            # 二重発注の疑い: 同一サイクル・同一ペアで 2 件目の約定として記録
            self._add_signal_event(
                ev, "order", pair=pair, strategy=p.get("strategy"), side=p.get("side"),
                result="EXECUTED", detail=f"複数positionId検出 {ev.data['count']}件",
            )
            self.counts["double"] += 1


# --------------------------------------------------------------------------- #
# 取り込み本体
# --------------------------------------------------------------------------- #
def ingest_once(log_path: str, state_path: str) -> dict[str, int]:
    if not os.path.exists(log_path):
        raise FileNotFoundError(log_path)

    state = _load_state(state_path)
    key = _state_key(log_path)
    offset = int(state.get(key, {}).get("offset", 0))
    if os.path.getsize(log_path) < offset:  # ローテーション検知 → 先頭から
        offset = 0

    session = SessionLocal()
    ing = Ingestor(session)
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            for line in fh:
                ing.counts["lines"] += 1
                ev = parse_line(line)
                if ev is None:
                    ing.counts["skipped"] += 1
                    continue
                ing.apply(ev)
            new_offset = fh.tell()
        if ing.counts["lines"] > 0:
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
    return ing.counts


def main() -> None:
    settings = get_settings()
    ap = argparse.ArgumentParser(description="fxbot.log を DB に取り込む")
    ap.add_argument("--path", default=settings.fxbot_log_path)
    ap.add_argument("--state", default=settings.ingest_state_path)
    ap.add_argument("--follow", action="store_true", help="間隔をあけて追記を取り込み続ける")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()

    init_db()
    if args.follow:
        print(f"follow モード: {args.path} を {args.interval}s 間隔で監視")
        while True:
            try:
                print(time.strftime("%H:%M:%S"), ingest_once(args.path, args.state))
            except FileNotFoundError:
                print("ログ未検出、待機中...")
            time.sleep(args.interval)
    else:
        print(ingest_once(args.path, args.state))


if __name__ == "__main__":
    main()
