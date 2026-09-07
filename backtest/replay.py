"""バックテスト再現 + トレード単位の差分。

手順:
  1. OHLC を戦略ロジック(`strategies.generate_all_signals`)に通し「期待シグナル」を再現
  2. 実運用の signal_events(event_type='signal')を読み出す
  3. (pair, strategy) 一致 & 時刻が許容内 で 1 件ずつマッチング
     - MATCHED  : 期待と実運用が対応し、方向も一致
     - MISMATCH : 対応したが方向が逆
     - MISSING  : 期待あり・実運用に対応なし(バックテストなら取っていたはずの取引を逃した)
     - EXTRA    : 実運用あり・期待に対応なし(バックテストにない発注)
  4. parity_runs に保存

使い方:
  python -m backtest.replay --ohlc path/to/ohlc.csv
  python -m backtest.replay --demo            # デモ OHLC で実行
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cache import clear as clear_cache
from app.db import SessionLocal, init_db
from app.models import ParityRun, SignalEvent

from . import ohlc as ohlc_mod
from .exits import simulate_trades
from .strategies import ExpectedSignal, generate_all_signals

MATCH_TOLERANCE = timedelta(minutes=90)  # 1 サイクル(hourly)+ 余裕


@dataclass
class DiffResult:
    matched: list[dict]
    mismatched: list[dict]
    missing: list[dict]
    extra: list[dict]

    @property
    def expected_n(self) -> int:
        return len(self.matched) + len(self.mismatched) + len(self.missing)

    @property
    def actual_n(self) -> int:
        return len(self.matched) + len(self.mismatched) + len(self.extra)

    @property
    def match_rate(self) -> float:
        return len(self.matched) / self.expected_n if self.expected_n else 0.0


def _load_live_signals(session: Session) -> list[dict]:
    rows = session.execute(
        select(
            SignalEvent.ts, SignalEvent.pair, SignalEvent.strategy, SignalEvent.side
        ).where(SignalEvent.event_type == "signal")
    ).all()
    out = []
    for ts, pair, strat, side in rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append({"ts": ts, "pair": pair, "strategy": strat, "side": side})
    return out


def diff_signals(
    expected: list[ExpectedSignal],
    actual: list[dict],
    tol: timedelta = MATCH_TOLERANCE,
) -> DiffResult:
    # (pair, strategy) ごとに時刻ソートしてグリーディにマッチ
    exp_by_key: dict[tuple, list] = defaultdict(list)
    for e in expected:
        exp_by_key[(e.pair, e.strategy)].append(e)
    act_by_key: dict[tuple, list] = defaultdict(list)
    for a in actual:
        act_by_key[(a["pair"], a["strategy"])].append(a)

    matched, mismatched, missing, extra = [], [], [], []

    for key in set(exp_by_key) | set(act_by_key):
        exps = sorted(exp_by_key.get(key, []), key=lambda e: e.ts)
        acts = sorted(act_by_key.get(key, []), key=lambda a: a["ts"])
        used = [False] * len(acts)
        for e in exps:
            best_j, best_gap = -1, None
            for j, a in enumerate(acts):
                if used[j]:
                    continue
                gap = abs(a["ts"] - e.ts)
                if gap <= tol and (best_gap is None or gap < best_gap):
                    best_j, best_gap = j, gap
            if best_j < 0:
                missing.append(_rec(e.ts, e.pair, e.strategy, e.side, None))
                continue
            used[best_j] = True
            a = acts[best_j]
            rec = _rec(e.ts, e.pair, e.strategy, e.side, a["side"])
            if a["side"] == e.side or a["side"] is None:
                matched.append(rec)
            else:
                mismatched.append(rec)
        for j, a in enumerate(acts):
            if not used[j]:
                extra.append(_rec(a["ts"], a["pair"], a["strategy"], None, a["side"]))

    for lst in (matched, mismatched, missing, extra):
        lst.sort(key=lambda r: (r["ts"], r["pair"]))
    return DiffResult(matched, mismatched, missing, extra)


def _rec(ts, pair, strat, exp_side, act_side) -> dict:
    return {
        "ts": ts.isoformat(),
        "pair": pair,
        "strategy": strat,
        "expected_side": exp_side,
        "actual_side": act_side,
    }


def _by_strategy(diff: DiffResult) -> list[dict]:
    agg: dict[str, dict] = defaultdict(lambda: {"matched": 0, "mismatch": 0, "missing": 0, "extra": 0})
    for r in diff.matched:
        agg[r["strategy"]]["matched"] += 1
    for r in diff.mismatched:
        agg[r["strategy"]]["mismatch"] += 1
    for r in diff.missing:
        agg[r["strategy"]]["missing"] += 1
    for r in diff.extra:
        agg[r["strategy"]]["extra"] += 1
    rows = []
    for strat, d in agg.items():
        exp = d["matched"] + d["mismatch"] + d["missing"]
        rows.append(
            {
                "strategy": strat,
                **d,
                "expected": exp,
                "match_rate": round(d["matched"] / exp, 4) if exp else None,
            }
        )
    order = {s: i for i, s in enumerate(["SB", "MACD", "DM_PSAR", "BB_SQ"])}
    rows.sort(key=lambda r: order.get(r["strategy"], 99))
    return rows


def run(ohlc: pd.DataFrame, session: Session, source: str) -> ParityRun:
    expected = generate_all_signals(ohlc)
    actual = _load_live_signals(session)

    # 実運用シグナルが存在する期間だけを比較対象にする(OHLC を warmup 用に長く
    # 渡しても、実運用ログの無い区間の期待シグナルを MISSING 扱いしない)。
    if actual:
        lo = min(a["ts"] for a in actual) - MATCH_TOLERANCE
        hi = max(a["ts"] for a in actual) + MATCH_TOLERANCE
        expected = [e for e in expected if lo <= e.ts <= hi]

    diff = diff_signals(expected, actual)

    detail = {
        "by_strategy": _by_strategy(diff),
        "samples": {
            "missing": diff.missing[:8],
            "extra": diff.extra[:8],
            "mismatch": diff.mismatched[:8],
        },
        "tolerance_minutes": int(MATCH_TOLERANCE.total_seconds() // 60),
        "note": (
            "戦略は仕様から起こした参照実装。厳密一致には本番のシグナル関数を"
            "共有モジュール化して import する必要がある(docs/PARITY.md)。"
        ),
    }
    pr = ParityRun(
        ran_at=datetime.now(timezone.utc),
        ohlc_source=source[:120],
        bars=int(len(ohlc)),
        period_start=pd.to_datetime(ohlc["ts"].min(), utc=True).to_pydatetime() if len(ohlc) else None,
        period_end=pd.to_datetime(ohlc["ts"].max(), utc=True).to_pydatetime() if len(ohlc) else None,
        expected_n=diff.expected_n,
        actual_n=diff.actual_n,
        matched_n=len(diff.matched),
        missing_n=len(diff.missing),
        extra_n=len(diff.extra),
        mismatch_n=len(diff.mismatched),
        match_rate=round(diff.match_rate, 4),
        detail_json=json.dumps(detail, ensure_ascii=False, default=str),
    )
    session.add(pr)
    session.commit()
    clear_cache()
    return pr


# ═══════════════════════════════════════════════════════════════════════════ #
# トレード単位(exit まで再現)の照合
# ═══════════════════════════════════════════════════════════════════════════ #
TRADE_MATCH_TOLERANCE = timedelta(hours=2)

_REASON_CAT = {
    "TP": "target", "SAFETY": "target",
    "SL": "loss_stop", "LOSSCUT": "loss_stop",
    "TRAIL": "trail",
    "TIMEOUT": "timeout", "CLOSED": "unknown",
}


def _reason_cat(r: str | None) -> str:
    return _REASON_CAT.get((r or "").upper(), "unknown")


def _load_actual_trades(session: Session) -> list[dict]:
    from app.models import Trade

    rows = session.query(Trade).filter(Trade.status == "closed").all()
    out = []
    for t in rows:
        et = t.entry_time.replace(tzinfo=timezone.utc) if t.entry_time and t.entry_time.tzinfo is None else t.entry_time
        xt = t.exit_time.replace(tzinfo=timezone.utc) if t.exit_time and t.exit_time.tzinfo is None else t.exit_time
        out.append({
            "pair": t.pair, "strategy": t.strategy, "side": t.side,
            "entry_time": et, "exit_time": xt,
            "pnl_jpy": t.pnl_jpy, "exit_reason": t.exit_reason, "risk_pct": t.risk_pct,
        })
    return out


def _actual_r(tr: dict, equity: pd.DataFrame) -> float | None:
    """実トレードの損益を R 換算(entry 時点の残高 × risk_pct を 1R とみなす)。"""
    if tr["pnl_jpy"] is None or not tr["risk_pct"]:
        return None
    bal = None
    if not equity.empty:
        e = equity[equity["ts"] <= pd.Timestamp(tr["entry_time"])]
        if not e.empty:
            bal = float(e.sort_values("ts")["balance_jpy"].iloc[-1])
    if not bal:
        return None
    one_r = tr["risk_pct"] * bal
    return round(tr["pnl_jpy"] / one_r, 3) if one_r else None


@dataclass
class TradeDiff:
    matched: list[dict]
    missing: list[dict]   # バックテストなら成立・実運用に無し
    extra: list[dict]     # 実運用にあり・バックテストに無し

    @property
    def outcome_agree_n(self) -> int:
        return sum(1 for m in self.matched if m["outcome_agree"])


def diff_trades(sim, actual: list[dict], equity: pd.DataFrame) -> TradeDiff:
    from collections import defaultdict

    sim_by = defaultdict(list)
    for s in sim:
        sim_by[(s.pair, s.strategy)].append(s)
    act_by = defaultdict(list)
    for a in actual:
        act_by[(a["pair"], a["strategy"])].append(a)

    matched, missing, extra = [], [], []
    for key in set(sim_by) | set(act_by):
        ss = sorted(sim_by.get(key, []), key=lambda s: s.entry_ts)
        aa = sorted(act_by.get(key, []), key=lambda a: a["entry_time"])
        used = [False] * len(aa)
        for s in ss:
            j, best = -1, None
            for k, a in enumerate(aa):
                if used[k]:
                    continue
                gap = abs(a["entry_time"] - s.entry_ts)
                if gap <= TRADE_MATCH_TOLERANCE and (best is None or gap < best):
                    j, best = k, gap
            if j < 0:
                missing.append({
                    "pair": s.pair, "strategy": s.strategy, "entry_ts": s.entry_ts.isoformat(),
                    "sim_r": s.pnl_r, "sim_reason": s.exit_reason,
                })
                continue
            used[j] = True
            a = aa[j]
            ar = _actual_r(a, equity)
            a_pnl_sign = 0 if a["pnl_jpy"] is None else (1 if a["pnl_jpy"] > 0 else -1)
            s_sign = 1 if s.pnl_r > 0 else -1
            hold_actual = None
            if a["exit_time"] and a["entry_time"]:
                hold_actual = round((a["exit_time"] - a["entry_time"]).total_seconds() / 3600, 1)
            matched.append({
                "pair": s.pair, "strategy": s.strategy, "entry_ts": s.entry_ts.isoformat(),
                "sim_r": s.pnl_r, "actual_r": ar,
                "sim_reason": s.exit_reason, "actual_reason": a["exit_reason"],
                "outcome_agree": (s_sign == a_pnl_sign) and a_pnl_sign != 0,
                "reason_agree": _reason_cat(s.exit_reason) == _reason_cat(a["exit_reason"]),
                "hold_bars_sim": s.hold_bars, "hold_hours_actual": hold_actual,
            })
        for k, a in enumerate(aa):
            if not used[k]:
                extra.append({
                    "pair": a["pair"], "strategy": a["strategy"],
                    "entry_ts": a["entry_time"].isoformat() if a["entry_time"] else None,
                    "actual_pnl_jpy": a["pnl_jpy"], "actual_reason": a["exit_reason"],
                })
    for lst in (matched, missing, extra):
        lst.sort(key=lambda r: (r["entry_ts"] or "", r["pair"]))
    return TradeDiff(matched, missing, extra)


def _trade_by_strategy(td: TradeDiff) -> list[dict]:
    from collections import defaultdict

    agg = defaultdict(lambda: {"matched": 0, "outcome_agree": 0, "reason_agree": 0, "missing": 0, "extra": 0})
    for m in td.matched:
        d = agg[m["strategy"]]
        d["matched"] += 1
        d["outcome_agree"] += int(m["outcome_agree"])
        d["reason_agree"] += int(m["reason_agree"])
    for m in td.missing:
        agg[m["strategy"]]["missing"] += 1
    for m in td.extra:
        agg[m["strategy"]]["extra"] += 1
    rows = []
    for strat, d in agg.items():
        rows.append({
            "strategy": strat, **d,
            "outcome_agree_rate": round(d["outcome_agree"] / d["matched"], 4) if d["matched"] else None,
        })
    order = {s: i for i, s in enumerate(["SB", "MACD", "DM_PSAR", "BB_SQ"])}
    rows.sort(key=lambda r: order.get(r["strategy"], 99))
    return rows


def run_trades(ohlc: pd.DataFrame, session: Session, source: str) -> ParityRun:
    from app.aggregations import _read as _agg_read

    expected = generate_all_signals(ohlc)
    actual = _load_actual_trades(session)
    if actual:
        lo = min(a["entry_time"] for a in actual) - TRADE_MATCH_TOLERANCE - timedelta(days=1)
        hi = max(a["entry_time"] for a in actual) + TRADE_MATCH_TOLERANCE
        expected = [e for e in expected if lo <= e.ts <= hi]

    sim = simulate_trades(ohlc, expected)
    equity = _agg_read("equity_snapshots", session.get_bind())
    if not equity.empty:
        equity["ts"] = pd.to_datetime(equity["ts"], utc=True)
    td = diff_trades(sim, actual, equity)

    sim_r = [s.pnl_r for s in sim]
    act_r = [m["actual_r"] for m in td.matched if m["actual_r"] is not None]
    n_matched = len(td.matched)
    # 一致率 = マッチしたトレードのうち勝敗が一致した割合(カバレッジとは分けて見る)
    match_rate = td.outcome_agree_n / n_matched if n_matched else 0.0
    coverage = n_matched / len(sim) if sim else 0.0
    detail = {
        "mode": "trade",
        "by_strategy": _trade_by_strategy(td),
        "aggregate": {
            "sim_trades": len(sim),
            "matched": n_matched,
            "coverage": round(coverage, 4),          # バックテスト想定のうち実際に取れた割合
            "avg_r_sim": round(float(np.mean(sim_r)), 3) if sim_r else None,
            "avg_r_actual_matched": round(float(np.mean(act_r)), 3) if act_r else None,
            "reason_agree_rate": (
                round(sum(m["reason_agree"] for m in td.matched) / n_matched, 4)
                if n_matched else None
            ),
        },
        "samples": {
            "missing": td.missing[:8],
            "extra": td.extra[:8],
            "mismatch": [m for m in td.matched if not m["outcome_agree"]][:8],
        },
        "note": (
            "エグジットは近似モデル(手法別RR/トレール規則 + 直近スイング±ATR の SL)。"
            "本番の SL/TP はシグナル関数内で計算されるため、exact 一致には strategy_core.py 化が必要。"
        ),
    }
    pr = ParityRun(
        ran_at=datetime.now(timezone.utc), mode="trade", ohlc_source=source[:120],
        bars=int(len(ohlc)),
        period_start=pd.to_datetime(ohlc["ts"].min(), utc=True).to_pydatetime() if len(ohlc) else None,
        period_end=pd.to_datetime(ohlc["ts"].max(), utc=True).to_pydatetime() if len(ohlc) else None,
        expected_n=len(sim), actual_n=len(actual),
        matched_n=len(td.matched), missing_n=len(td.missing), extra_n=len(td.extra),
        mismatch_n=sum(1 for m in td.matched if not m["outcome_agree"]),
        match_rate=round(match_rate, 4),
        detail_json=json.dumps(detail, ensure_ascii=False, default=str),
    )
    session.add(pr)
    session.commit()
    clear_cache()
    return pr


PAIRS = [
    "USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY", "EUR_USD", "GBP_USD",
    "AUD_USD", "NZD_JPY", "CAD_JPY", "CHF_JPY", "EUR_GBP", "NZD_USD",
]


def _parse_day(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if s else None


def main() -> None:
    from datetime import timedelta

    ap = argparse.ArgumentParser(description="バックテスト再現(トレード単位照合)")
    ap.add_argument("--ohlc", help="OHLC CSV パス(単一ファイル。ts,pair,... か time,Open,... )")
    ap.add_argument("--fx-cache", help="1 ペア 1 ファイルの OHLC ディレクトリ(例: ../fx_cache = HistData)")
    ap.add_argument("--yfinance", action="store_true",
                    help="本番ボットと同じ Yahoo Finance 1時間足を取得(要 pip install yfinance)")
    ap.add_argument("--start", help="開始日 YYYY-MM-DD(--fx-cache / --yfinance と併用)")
    ap.add_argument("--end", help="終了日 YYYY-MM-DD(--fx-cache / --yfinance と併用)")
    ap.add_argument("--demo", action="store_true", help="デモ OHLC を生成して実行")
    ap.add_argument("--mode", choices=["signal", "trade", "both"], default="both",
                    help="signal=entry シグナル照合 / trade=exit まで再現した損益照合 / both(既定)")
    args = ap.parse_args()

    init_db()
    if args.demo:
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        ohlc = ohlc_mod.make_demo_ohlc(PAIRS, now - timedelta(days=120), now)
        source = "demo (synthetic OHLC, 120d/1h)"
    elif args.yfinance:
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = _parse_day(args.start) or (now - timedelta(days=60))
        end = _parse_day(args.end) or now
        ohlc = ohlc_mod.load_yfinance(PAIRS, start, end)
        source = f"yfinance 1h {start:%Y-%m-%d}〜{end:%Y-%m-%d}"
    elif args.fx_cache:
        ohlc = ohlc_mod.load_dir(
            args.fx_cache, PAIRS, _parse_day(args.start), _parse_day(args.end)
        )
        _dir = os.path.basename(args.fx_cache.rstrip("/\\")) or args.fx_cache
        source = f"{_dir} {args.start or 'all'}〜{args.end or 'all'}"
    elif args.ohlc:
        ohlc = ohlc_mod.load_csv(args.ohlc)
        source = os.path.basename(args.ohlc)
    else:
        ap.error("--ohlc / --fx-cache / --yfinance / --demo のいずれかを指定してください")
        return

    session = SessionLocal()
    try:
        if args.mode in ("signal", "both"):
            pr = run(ohlc, session, source)
            print(
                f"[signal] #{pr.id}: expected={pr.expected_n} actual={pr.actual_n} "
                f"matched={pr.matched_n} missing={pr.missing_n} extra={pr.extra_n} "
                f"mismatch={pr.mismatch_n} match_rate={pr.match_rate:.1%}"
            )
        if args.mode in ("trade", "both"):
            pr = run_trades(ohlc, session, source)
            print(
                f"[trade]  #{pr.id}: sim_trades={pr.expected_n} actual_trades={pr.actual_n} "
                f"matched={pr.matched_n} missing={pr.missing_n} extra={pr.extra_n} "
                f"outcome_disagree={pr.mismatch_n} outcome_agree_rate={pr.match_rate:.1%}"
            )
    finally:
        session.close()


if __name__ == "__main__":
    main()
