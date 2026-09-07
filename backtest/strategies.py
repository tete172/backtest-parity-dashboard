"""4 手法のシグナル生成(参照実装)。

⚠️ 本リポジトリは公開ポートフォリオです。ここの手法名・パラメータは
**一般的な教科書値に置き換えたサンプル**であり、監視対象ボットで実際に使っている
チューニング済みの値ではありません(実値は非公開)。照合の仕組み・データ構造・
テスト・アーキテクチャを示すことが目的で、戦略そのものの再現は目的にしていません。

厳密なトレード単位一致を取るには、監視対象ボットのシグナル判定を純粋関数として
共有モジュールに切り出し、ボットとこのリプレイの両方が import する構成にする。
詳細は `docs/PARITY.md`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from . import indicators as ind

# --- サンプルパラメータ(教科書値。実値ではない)---
SB_BB_N, SB_BB_K = 20, 2.0     # ボリンジャーバンド 20/2
SB_SQUEEZE_Q = 0.30            # BB 幅が過去分布の下位 30% ならスクイーズ
SB_MACD = (50, 200, 20)       # EMA50/200、シグナル 20

DM_MACD1 = (50, 150, 15)
DM_MACD2 = (100, 300, 30)
DM_MIN_CONSOL, DM_MAX_CONSOL = 4, 30

BB_SQ_N = 6
BB_MIN_CONSOL, BB_MAX_CONSOL = 6, 24

MACDX = (12, 26, 9)           # 教科書 MACD

STRATEGY_PRIORITY = ["SB", "MACD", "DM_PSAR", "BB_SQ"]
COOLDOWN_BARS = 18           # 1 ペア 1 ポジションの近似(taken 後この本数は新規を抑制)
WARMUP_BARS = 320            # 最長 EMA300 が立ち上がるまでシグナルを出さない


@dataclass(frozen=True)
class ExpectedSignal:
    ts: datetime
    pair: str
    strategy: str
    side: str  # LONG / SHORT


def _indicator_frame(g: pd.DataFrame) -> pd.DataFrame:
    close, high, low = g["close"], g["high"], g["low"]
    f = pd.DataFrame(index=g.index)
    bb = ind.bollinger(close, SB_BB_N, SB_BB_K)
    f["bb_up"], f["bb_lo"], f["bb_mid"], f["bb_w"] = bb["upper"], bb["lower"], bb["mid"], bb["width"]
    f["bb_w_q"] = bb["width"].rolling(120, min_periods=40).quantile(SB_SQUEEZE_Q)
    m_sb = ind.macd(close, *SB_MACD)
    f["sb_hist"] = m_sb["hist"]
    m1, m2 = ind.macd(close, *DM_MACD1), ind.macd(close, *DM_MACD2)
    f["dm1"], f["dm2"] = m1["hist"], m2["hist"]
    f["dm_consol"] = ind.bars_since_sign_change(m1["hist"])
    mx = ind.macd(close, *MACDX)
    f["mx"], f["mx_sig"] = mx["macd"], mx["signal"]
    ps = ind.psar(high, low)
    f["psar_dir"] = ps["dir"].values
    f["bbsq_consol"] = ind.bars_since_sign_change((bb["width"] - f["bb_w_q"]).where(lambda s: s < 0, 1.0))
    f["prev_close"] = close.shift(1)
    return f


def sb_signals(g: pd.DataFrame, f: pd.DataFrame) -> list[tuple[int, str]]:
    squeeze_prev = (f["bb_w"].shift(1) < f["bb_w_q"].shift(1))
    long_ = squeeze_prev & (g["close"] > f["bb_up"]) & (f["prev_close"] <= f["bb_up"].shift(1)) & (f["sb_hist"] > 0)
    short_ = squeeze_prev & (g["close"] < f["bb_lo"]) & (f["prev_close"] >= f["bb_lo"].shift(1)) & (f["sb_hist"] < 0)
    return _collect(long_, short_)


def macd_bb_sar_signals(g: pd.DataFrame, f: pd.DataFrame) -> list[tuple[int, str]]:
    cross_up = (f["mx"] > f["mx_sig"]) & (f["mx"].shift(1) <= f["mx_sig"].shift(1))
    cross_dn = (f["mx"] < f["mx_sig"]) & (f["mx"].shift(1) >= f["mx_sig"].shift(1))
    long_ = cross_up & (g["close"] > f["bb_mid"]) & (f["psar_dir"] > 0)
    short_ = cross_dn & (g["close"] < f["bb_mid"]) & (f["psar_dir"] < 0)
    return _collect(long_, short_)


def dm_psar_signals(g: pd.DataFrame, f: pd.DataFrame) -> list[tuple[int, str]]:
    consol_ok = f["dm_consol"].between(DM_MIN_CONSOL, DM_MAX_CONSOL)
    long_ = (f["dm1"] > 0) & (f["dm2"] > 0) & (f["psar_dir"] > 0) & consol_ok
    short_ = (f["dm1"] < 0) & (f["dm2"] < 0) & (f["psar_dir"] < 0) & consol_ok
    # トレンド継続中の連続シグナルは cooldown で間引く
    return _collect(long_, short_)


def bb_sq_signals(g: pd.DataFrame, f: pd.DataFrame) -> list[tuple[int, str]]:
    consol_ok = f["bbsq_consol"].between(BB_MIN_CONSOL, BB_MAX_CONSOL)
    brk_up = (g["close"] > f["bb_up"]) & (f["prev_close"] <= f["bb_up"].shift(1))
    brk_dn = (g["close"] < f["bb_lo"]) & (f["prev_close"] >= f["bb_lo"].shift(1))
    return _collect(consol_ok & brk_up, consol_ok & brk_dn)


def _collect(long_mask: pd.Series, short_mask: pd.Series) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    lm = long_mask.fillna(False).to_numpy()
    sm = short_mask.fillna(False).to_numpy()
    for i in range(len(lm)):
        if lm[i]:
            out.append((i, "LONG"))
        elif sm[i]:
            out.append((i, "SHORT"))
    return out


_FNS = {
    "SB": sb_signals,
    "MACD": macd_bb_sar_signals,
    "DM_PSAR": dm_psar_signals,
    "BB_SQ": bb_sq_signals,
}


def generate_all_signals(ohlc: pd.DataFrame) -> list[ExpectedSignal]:
    """全ペア・全手法のエントリーシグナルを、優先順位 + 1 ペア 1 ポジション近似で確定する。"""
    raw: list[tuple[datetime, str, str, str, int]] = []  # ts, pair, strat, side, prio
    prio = {s: i for i, s in enumerate(STRATEGY_PRIORITY)}
    for pair, g in ohlc.groupby("pair"):
        g = g.sort_values("ts").reset_index(drop=True)
        f = _indicator_frame(g)
        for strat, fn in _FNS.items():
            for i, side in fn(g, f):
                if i < WARMUP_BARS:
                    continue
                raw.append((g["ts"].iloc[i], pair, strat, side, prio[strat]))

    # 同一(ペア, バー)は最優先手法のみ
    best: dict[tuple, tuple] = {}
    for ts, pair, strat, side, p in raw:
        key = (pair, ts)
        if key not in best or p < best[key][3]:
            best[key] = (strat, side, ts, p, pair)

    # ペアごとに時系列で cooldown を適用(1 ペア 1 ポジションの近似)
    per_pair: dict[str, list] = {}
    for (pair, ts), (strat, side, _ts, _p, _pair) in best.items():
        per_pair.setdefault(pair, []).append((ts, strat, side))
    out: list[ExpectedSignal] = []
    for pair, items in per_pair.items():
        items.sort(key=lambda x: x[0])
        last_idx_ts = None
        # cooldown はバー本数なので、そのペアの ts 配列で位置を引く
        ts_list = sorted({t for t, *_ in items})
        pos = {t: k for k, t in enumerate(ts_list)}
        last_pos = -(10**9)
        for ts, strat, side in items:
            if pos[ts] - last_pos < COOLDOWN_BARS:
                continue
            out.append(ExpectedSignal(ts=ts, pair=pair, strategy=strat, side=side))
            last_pos = pos[ts]
    out.sort(key=lambda e: (e.ts, e.pair))
    return out
