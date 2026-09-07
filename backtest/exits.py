"""エグジット再現(近似モデル)。

エントリーシグナル(`strategies.ExpectedSignal`)を起点に、その後の OHLC を 1 本ずつ進めて
SL / TP / トレールのどれで抜けたかを判定し、「バックテストなら成立していたトレード」
(entry / exit / R 損益 / 決済理由 / 保有本数)を返す。

⚠️ 本リポジトリは公開ポートフォリオ。ここの RR / トレール規則は**サンプル(一般的な値)**で、
監視対象ボットの実値ではない(実値は非公開)。監視対象ボットでは SL/TP は各手法の
シグナル関数の中で計算されるため、ここでは共通の「直近スイング ± ATR バッファ」で SL を置き、
手法ごとの RR / トレール規則を当てる近似モデルとしている。
  SB / BB_SQ … 固定OCO(RR = 2.0)
  MACD       … 固定OCO(RR = 2.0)
  DM_PSAR    … トレール(1R 起動・1R 刻み、6R セーフティ)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from . import indicators as ind
from .strategies import ExpectedSignal

# 手法別のリワード・リスク / トレール規則(サンプル値)
RR = {"SB": 2.0, "MACD": 2.0, "BB_SQ": 2.0}
DM_TRAIL_ACT_R = 1.0     # +1R で建値化・トレール起動
DM_TRAIL_STEP_R = 1.0    # 以後 1R 刻みで SL を引き上げ
DM_SAFETY_R = 6.0        # トレール手法のセーフティ TP

SWING_LOOKBACK = 10      # SL を置く直近スイングの本数
ATR_BUFFER = 0.2         # SL = スイング ± ATR×0.2(本番 PIN_BUF_MULT と同じ発想)
ATR_N = 14
MAX_HOLD_BARS = 96       # これを超えたら成行手仕舞い(TIMEOUT。4 日相当)
PIP = {"JPY": 0.01, "OTHER": 0.0001}
SLIPPAGE_PIP = 0.5


@dataclass
class SimTrade:
    pair: str
    strategy: str
    side: str            # LONG / SHORT
    signal_ts: datetime  # シグナル発生バーの時刻(entry_ts はその次バー)
    entry_ts: datetime
    entry_px: float
    exit_ts: datetime
    exit_px: float
    initial_risk: float  # |entry - 初期SL|(価格幅)
    pnl_r: float         # R 倍率(スリッページ控除後)
    pnl_pips: float
    exit_reason: str     # TP / SL / TRAIL / SAFETY / TIMEOUT
    hold_bars: int


def _pip(pair: str) -> float:
    return PIP["JPY"] if pair.endswith("JPY") else PIP["OTHER"]


def simulate_one(
    g: pd.DataFrame, atr: pd.Series, i0: int, sig: ExpectedSignal
) -> SimTrade | None:
    """g は 1 ペアの時系列(ts,open,high,low,close 昇順・index リセット済み)。i0 = シグナル発生バー。"""
    n = len(g)
    if i0 + 1 >= n:
        return None
    entry_i = i0 + 1                       # 次バー始値で約定(bot は完了足で判断 → 次足で発注)
    entry_px = float(g["open"].iloc[entry_i])
    a = float(atr.iloc[i0]) if not np.isnan(atr.iloc[i0]) else _pip(sig.pair) * 20
    long_ = sig.side == "LONG"

    if long_:
        swing = float(g["low"].iloc[max(0, i0 - SWING_LOOKBACK):i0 + 1].min())
        sl = min(swing - a * ATR_BUFFER, entry_px - a * 0.5)
        risk = entry_px - sl
    else:
        swing = float(g["high"].iloc[max(0, i0 - SWING_LOOKBACK):i0 + 1].max())
        sl = max(swing + a * ATR_BUFFER, entry_px + a * 0.5)
        risk = sl - entry_px
    if risk <= 0:
        return None

    is_trail = sig.strategy == "DM_PSAR"
    tp = None
    if not is_trail:
        rr = RR.get(sig.strategy, 2.0)
        tp = entry_px + rr * risk if long_ else entry_px - rr * risk
    safety = entry_px + DM_SAFETY_R * risk if long_ else entry_px - DM_SAFETY_R * risk

    cur_sl = sl
    for j in range(entry_i, min(n, entry_i + MAX_HOLD_BARS)):
        hi = float(g["high"].iloc[j])
        lo = float(g["low"].iloc[j])

        # トレール SL の更新(バー高値/安値ベースの含み益で段階引き上げ)
        if is_trail:
            mfe = (hi - entry_px) if long_ else (entry_px - lo)
            steps = int(mfe / risk // DM_TRAIL_STEP_R)
            if steps >= DM_TRAIL_ACT_R:
                trailed = (entry_px + (steps - 1) * risk) if long_ else (entry_px - (steps - 1) * risk)
                cur_sl = max(cur_sl, trailed) if long_ else min(cur_sl, trailed)

        hit_sl = lo <= cur_sl if long_ else hi >= cur_sl
        hit_tp = (tp is not None) and (hi >= tp if long_ else lo <= tp)
        hit_safety = hi >= safety if long_ else lo <= safety

        if hit_sl:  # 同一バーで SL/TP 両方なら SL 優先(保守)
            reason = "TRAIL" if (is_trail and cur_sl != sl) else "SL"
            return _finish(sig, g, entry_i, j, entry_px, cur_sl, risk, reason)
        if hit_tp:
            return _finish(sig, g, entry_i, j, entry_px, tp, risk, "TP")
        if hit_safety:
            return _finish(sig, g, entry_i, j, entry_px, safety, risk, "SAFETY")

    j = min(n - 1, entry_i + MAX_HOLD_BARS - 1)
    return _finish(sig, g, entry_i, j, entry_px, float(g["close"].iloc[j]), risk, "TIMEOUT")


def _finish(sig, g, entry_i, j, entry_px, exit_px, risk, reason) -> SimTrade:
    long_ = sig.side == "LONG"
    pip = _pip(sig.pair)
    slip = SLIPPAGE_PIP * pip
    gross = (exit_px - entry_px) if long_ else (entry_px - exit_px)
    net = gross - 2 * slip                      # entry + exit のスリッページ
    return SimTrade(
        pair=sig.pair, strategy=sig.strategy, side=sig.side,
        signal_ts=pd.Timestamp(sig.ts).to_pydatetime(),
        entry_ts=g["ts"].iloc[entry_i].to_pydatetime(),
        entry_px=round(entry_px, 5),
        exit_ts=g["ts"].iloc[j].to_pydatetime(),
        exit_px=round(exit_px, 5),
        initial_risk=round(risk, 5),
        pnl_r=round(net / risk, 3),
        pnl_pips=round(net / pip, 1),
        exit_reason=reason,
        hold_bars=j - entry_i,
    )


def simulate_trades(
    ohlc: pd.DataFrame, expected: list[ExpectedSignal]
) -> list[SimTrade]:
    by_pair: dict[str, list[ExpectedSignal]] = {}
    for e in expected:
        by_pair.setdefault(e.pair, []).append(e)

    out: list[SimTrade] = []
    for pair, sigs in by_pair.items():
        g = ohlc[ohlc["pair"] == pair].sort_values("ts").reset_index(drop=True)
        if len(g) < 30:
            continue
        atr = ind.atr(g["high"], g["low"], g["close"], ATR_N)
        ts_to_i = {t: k for k, t in enumerate(g["ts"])}
        for sig in sigs:
            i0 = ts_to_i.get(pd.Timestamp(sig.ts))
            if i0 is None:
                continue
            st = simulate_one(g, atr, i0, sig)
            if st is not None:
                out.append(st)
    out.sort(key=lambda s: (s.entry_ts, s.pair))
    return out
