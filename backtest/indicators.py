"""テクニカル指標(ベクトル化)。1 ペアぶんの時系列 DataFrame を受け取る。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def macd(close: pd.Series, fast: int, slow: int, signal: int) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def bollinger(close: pd.Series, n: int, k: float) -> pd.DataFrame:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    upper = mid + k * sd
    lower = mid - k * sd
    width = (upper - lower) / mid.replace(0, np.nan)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "width": width})


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(n).mean()


def psar(high: pd.Series, low: pd.Series, af_step: float = 0.02, af_max: float = 0.2) -> pd.DataFrame:
    """Parabolic SAR。dir は +1(上昇トレンド)/ -1(下降トレンド)。"""
    h = high.to_numpy(dtype="float64")
    l = low.to_numpy(dtype="float64")
    n = len(h)
    sar = np.full(n, np.nan)
    direction = np.zeros(n, dtype="int8")
    if n < 2:
        return pd.DataFrame({"sar": sar, "dir": direction}, index=high.index)

    up = True
    af = af_step
    ep = h[0]
    sar[0] = l[0]
    direction[0] = 1
    for i in range(1, n):
        prev = sar[i - 1]
        if up:
            cur = prev + af * (ep - prev)
            cur = min(cur, l[i - 1], l[i - 2] if i >= 2 else l[i - 1])
            if l[i] < cur:
                up = False
                cur = ep
                ep = l[i]
                af = af_step
            else:
                if h[i] > ep:
                    ep = h[i]
                    af = min(af + af_step, af_max)
        else:
            cur = prev + af * (ep - prev)
            cur = max(cur, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > cur:
                up = True
                cur = ep
                ep = h[i]
                af = af_step
            else:
                if l[i] < ep:
                    ep = l[i]
                    af = min(af + af_step, af_max)
        sar[i] = cur
        direction[i] = 1 if up else -1
    return pd.DataFrame({"sar": sar, "dir": direction}, index=high.index)


def bars_since_sign_change(x: pd.Series) -> pd.Series:
    """符号が最後に変わってからの経過本数(もみ合い/トレンド継続の長さの近似)。"""
    sign = np.sign(x.fillna(0.0)).to_numpy()
    out = np.zeros(len(sign), dtype="int64")
    count = 0
    for i in range(1, len(sign)):
        if sign[i] != 0 and sign[i] == sign[i - 1]:
            count += 1
        else:
            count = 0
        out[i] = count
    return pd.Series(out, index=x.index)
