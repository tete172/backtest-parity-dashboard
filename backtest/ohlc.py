"""OHLC(1 時間足)の入出力。

対応する 2 つの CSV 形式(列名の大小・順序は自動判別):

1. ロング形式(このリポジトリの標準):  ts,pair,open,high,low,close
2. バックテスト用チャート形式(fx_cache/USD_JPY.csv 等):  time,Open,High,Low,Close,Volume
   → 1 ファイル 1 ペア。ペア名はファイル名(拡張子除く)から取る。time は naive → UTC とみなす。

価格フィードの前提(重要):
- 本番ボット `監視対象ボット` はシグナル判定を **Yahoo Finance の 1時間足**
  (`yf.download(interval='1h', period='60d')`)で行い、約定価格だけ GMO ticker を使う。
- バックテストの `fx_cache/*.csv` は **HistData.com**(`DAT_ASCII_<PAIR>_M1_<YEAR>` を1h化)。
- したがって厳密な parity には `load_yfinance()`(ボットと同じ Yahoo Finance)を使う。
  詳細は docs/PARITY.md。

対応する CSV 形式(列名の大小・順序は自動判別):
1. ロング形式(このリポジトリの標準):  ts,pair,open,high,low,close
2. fx_cache 形式:  time,Open,High,Low,Close,Volume(1ファイル1ペア、ペア名はファイル名)
"""

from __future__ import annotations

import glob
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

COLUMNS = ["ts", "pair", "open", "high", "low", "close"]

# 監視対象ボット の GMO_TO_YF と同一(コピー)
GMO_TO_YF = {
    "USD_JPY": "JPY=X", "EUR_JPY": "EURJPY=X", "GBP_JPY": "GBPJPY=X",
    "AUD_JPY": "AUDJPY=X", "NZD_JPY": "NZDJPY=X",
    "EUR_USD": "EURUSD=X", "GBP_USD": "GBPUSD=X",
    "AUD_USD": "AUDUSD=X", "NZD_USD": "NZDUSD=X",
    "CAD_JPY": "CADJPY=X", "EUR_GBP": "EURGBP=X", "CHF_JPY": "CHFJPY=X",
}
_RENAME = {
    "time": "ts", "date": "ts", "datetime": "ts", "timestamp": "ts",
    "open": "open", "high": "high", "low": "low", "close": "close",
}


def _normalize(df: pd.DataFrame, pair: str | None) -> pd.DataFrame:
    cols = {c.lower(): c for c in df.columns}
    ren = {cols[k]: v for k, v in _RENAME.items() if k in cols}
    df = df.rename(columns=ren)
    if "pair" not in df.columns:
        if pair is None:
            raise ValueError("pair 列が無く、ファイル名からも特定できません")
        df["pair"] = pair
    for c in ("open", "high", "low", "close"):
        if c not in df.columns:
            raise ValueError(f"OHLC 列が不足: {c}")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df[COLUMNS].sort_values(["pair", "ts"]).reset_index(drop=True)


def load_csv(path: str, pair: str | None = None) -> pd.DataFrame:
    if pair is None:
        stem = os.path.splitext(os.path.basename(path))[0]
        if len(stem) in (6, 7) and "_" in stem:  # "USD_JPY" 等
            pair = stem.upper()
    return _normalize(pd.read_csv(path), pair)


def load_dir(
    directory: str,
    pairs: list[str],
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """fx_cache/ のような「1 ペア 1 ファイル」ディレクトリから複数ペアを読み込む。"""
    lo = pd.Timestamp(start).tz_localize("UTC") if start and pd.Timestamp(start).tzinfo is None else (pd.Timestamp(start) if start else None)
    hi = pd.Timestamp(end).tz_localize("UTC") if end and pd.Timestamp(end).tzinfo is None else (pd.Timestamp(end) if end else None)
    frames = []
    for p in pairs:
        matches = glob.glob(os.path.join(directory, f"{p}.csv")) or glob.glob(
            os.path.join(directory, f"{p.replace('_', '')}.csv")
        )
        if not matches:
            continue
        d = load_csv(matches[0], pair=p)
        if lo is not None:
            d = d[d["ts"] >= lo]
        if hi is not None:
            d = d[d["ts"] <= hi]
        frames.append(d)
    if not frames:
        raise FileNotFoundError(f"{directory} に対象ペアの CSV が見つかりません: {pairs}")
    return pd.concat(frames, ignore_index=True)


def load_yfinance(
    pairs: list[str],
    start: datetime,
    end: datetime,
    interval: str = "1h",
) -> pd.DataFrame:
    """本番ボットと同じ Yahoo Finance の 1時間足を取得する(要 `pip install yfinance`)。

    Yahoo の hourly データは直近 ~730 日に限られる。ボットは `period='60d'` で毎回取り直すため、
    parity を取りたい期間はなるべく直近にする。
    """
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("yfinance 未インストール。`pip install yfinance` を実行してください") from exc

    frames = []
    for p in pairs:
        sym = GMO_TO_YF.get(p)
        if sym is None:
            continue
        raw = yf.download(
            sym, interval=interval, start=start, end=end,
            progress=False, auto_adjust=True, threads=False,
        )
        if raw is None or raw.empty:
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.droplevel(-1)
        raw = raw.rename(columns=str.lower).reset_index()
        tcol = "datetime" if "datetime" in raw.columns else raw.columns[0]
        raw = raw.rename(columns={tcol: "ts"})
        raw["pair"] = p
        raw["ts"] = pd.to_datetime(raw["ts"], utc=True)
        frames.append(raw[COLUMNS])
    if not frames:
        raise RuntimeError("yfinance から OHLC を取得できませんでした")
    return pd.concat(frames, ignore_index=True)


def make_demo_ohlc(
    pairs: list[str],
    start: datetime,
    end: datetime,
    seed: int = 20260907,
) -> pd.DataFrame:
    """ボラティリティのクラスタリングを持つ疑似ランダムウォークで 1 時間足を生成。"""
    rng = np.random.default_rng(seed)
    hours = int((end - start).total_seconds() // 3600)
    idx = [start + timedelta(hours=h) for h in range(hours)]
    frames = []
    for p in pairs:
        base = 145.0 if p.endswith("JPY") else 1.25
        vol = (0.0025 if p.endswith("JPY") else 0.0018) * base
        # ボラのレジーム(緩やか→急変を繰り返す)
        regime = np.abs(rng.normal(1.0, 0.6, hours))
        regime = pd.Series(regime).rolling(48, min_periods=1).mean().to_numpy()
        rets = rng.normal(0, 1, hours) * vol * regime
        # 弱いトレンド成分
        rets += np.sin(np.linspace(0, rng.uniform(6, 20), hours)) * vol * 0.3
        close = base + np.cumsum(rets)
        open_ = np.concatenate([[base], close[:-1]])
        noise = np.abs(rng.normal(0, vol * 0.6, hours))
        high = np.maximum(open_, close) + noise
        low = np.minimum(open_, close) - noise
        frames.append(
            pd.DataFrame(
                {
                    "ts": idx,
                    "pair": p,
                    "open": np.round(open_, 5),
                    "high": np.round(high, 5),
                    "low": np.round(low, 5),
                    "close": np.round(close, 5),
                }
            )
        )
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df[COLUMNS]


if __name__ == "__main__":
    # ざっくり動作確認 + サンプル CSV 生成
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    demo = make_demo_ohlc(
        ["USD_JPY", "EUR_USD", "GBP_JPY"], now - timedelta(days=20), now
    )
    demo.to_csv("backtest/sample_ohlc.csv", index=False)
    print(demo.groupby("pair").size())
