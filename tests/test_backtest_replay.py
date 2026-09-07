"""バックテスト再現(トレード単位照合)のテスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backtest import exits as exits_mod
from backtest import ohlc as ohlc_mod
from backtest import replay
from backtest.strategies import ExpectedSignal, generate_all_signals
from app.models import Base, ParityRun, SignalEvent, Trade


def _dt(h: int) -> datetime:
    return datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(hours=h)


# --------------------------------------------------------------------------- #
# diff_signals の分類
# --------------------------------------------------------------------------- #
def test_diff_classifies_matched_missing_extra_mismatch():
    expected = [
        ExpectedSignal(_dt(0), "USD_JPY", "SB", "LONG"),      # → MATCHED
        ExpectedSignal(_dt(5), "USD_JPY", "SB", "SHORT"),     # → MISMATCH(実は LONG)
        ExpectedSignal(_dt(9), "EUR_USD", "MACD", "LONG"),    # → MISSING
    ]
    actual = [
        {"ts": _dt(0) + timedelta(minutes=20), "pair": "USD_JPY", "strategy": "SB", "side": "LONG"},
        {"ts": _dt(5), "pair": "USD_JPY", "strategy": "SB", "side": "LONG"},
        {"ts": _dt(30), "pair": "GBP_JPY", "strategy": "DM_PSAR", "side": "SHORT"},  # → EXTRA
    ]
    d = replay.diff_signals(expected, actual)
    assert len(d.matched) == 1
    assert len(d.mismatched) == 1
    assert len(d.missing) == 1
    assert len(d.extra) == 1
    assert d.expected_n == 3
    assert d.match_rate == pytest.approx(1 / 3)


def test_diff_time_tolerance():
    expected = [ExpectedSignal(_dt(0), "USD_JPY", "SB", "LONG")]
    # 許容(90分)を超えた actual はマッチしない
    actual = [{"ts": _dt(3), "pair": "USD_JPY", "strategy": "SB", "side": "LONG"}]
    d = replay.diff_signals(expected, actual)
    assert len(d.missing) == 1 and len(d.extra) == 1 and len(d.matched) == 0


# --------------------------------------------------------------------------- #
# OHLC ローダ(2 形式)
# --------------------------------------------------------------------------- #
def test_load_csv_long_format(tmp_path):
    p = tmp_path / "x.csv"
    pd.DataFrame(
        {
            "ts": ["2026-06-01T00:00:00Z", "2026-06-01T01:00:00Z"],
            "pair": ["USD_JPY", "USD_JPY"],
            "open": [147.0, 147.1], "high": [147.3, 147.2],
            "low": [146.9, 147.0], "close": [147.1, 147.15],
        }
    ).to_csv(p, index=False)
    df = ohlc_mod.load_csv(str(p))
    assert list(df.columns) == ohlc_mod.COLUMNS
    assert len(df) == 2 and df["pair"].iloc[0] == "USD_JPY"


def test_gmo_to_yf_map_matches_bot():
    # 監視対象ボット の GMO_TO_YF と同一であること(USD_JPY→JPY=X の癖含む)
    m = ohlc_mod.GMO_TO_YF
    assert m["USD_JPY"] == "JPY=X"
    assert m["EUR_GBP"] == "EURGBP=X"
    assert set(m) == set(replay.PAIRS)
    assert all(v.endswith("=X") for v in m.values())


def test_load_csv_fxcache_format(tmp_path):
    # fx_cache/USD_JPY.csv 形式: time,Open,High,Low,Close,Volume・ペア名はファイル名から
    p = tmp_path / "EUR_USD.csv"
    pd.DataFrame(
        {
            "time": ["2026-06-01 00:00:00", "2026-06-01 01:00:00"],
            "Open": [1.10, 1.101], "High": [1.103, 1.102],
            "Low": [1.099, 1.100], "Close": [1.101, 1.1015], "Volume": [500, 610],
        }
    ).to_csv(p, index=False)
    df = ohlc_mod.load_csv(str(p))
    assert df["pair"].iloc[0] == "EUR_USD"
    assert df["close"].iloc[1] == pytest.approx(1.1015)
    assert str(df["ts"].dt.tz) == "UTC"


# --------------------------------------------------------------------------- #
# generate_all_signals / run
# --------------------------------------------------------------------------- #
def test_generate_all_signals_shape():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    demo = ohlc_mod.make_demo_ohlc(["USD_JPY", "EUR_USD"], now - timedelta(days=90), now)
    sigs = generate_all_signals(demo)
    assert isinstance(sigs, list)
    for s in sigs[:20]:
        assert s.side in ("LONG", "SHORT")
        assert s.strategy in ("SB", "MACD", "DM_PSAR", "BB_SQ")
    # 時系列昇順
    assert all(sigs[i].ts <= sigs[i + 1].ts for i in range(len(sigs) - 1))


def _mk_ohlc_uptrend(pair="USD_JPY", bars=400):
    ts = pd.date_range("2026-05-01", periods=bars, freq="1h", tz="UTC")
    base = 150.0
    close = base + pd.Series(range(bars)) * 0.01  # 緩やかな上昇
    return pd.DataFrame({
        "ts": ts, "pair": pair,
        "open": close.shift(1).fillna(base).values,
        "high": (close + 0.05).values, "low": (close - 0.05).values, "close": close.values,
    })


def test_simulate_one_long_hits_tp_on_uptrend():
    g = _mk_ohlc_uptrend()
    from backtest import indicators as ind
    atr = ind.atr(g["high"], g["low"], g["close"], 14)
    sig = ExpectedSignal(g["ts"].iloc[100], "USD_JPY", "SB", "LONG")
    st = exits_mod.simulate_one(g, atr, 100, sig)
    assert st is not None
    assert st.side == "LONG"
    assert st.exit_reason in ("TP", "SL", "TRAIL", "SAFETY", "TIMEOUT")
    assert st.entry_ts > sig.ts                 # 次バーで約定
    assert st.exit_ts >= st.entry_ts
    # 上昇トレンドの LONG なので基本は勝ち(R>0)
    assert st.pnl_r > 0


def test_diff_trades_missing_extra_matched():
    from datetime import datetime, timezone

    def _dt(h):
        return datetime(2026, 5, 1, tzinfo=timezone.utc) + timedelta(hours=h)

    def _st(pair, strat, side, sig_h, ent_h, r):
        return exits_mod.SimTrade(
            pair=pair, strategy=strat, side=side, signal_ts=_dt(sig_h), entry_ts=_dt(ent_h),
            entry_px=100.0, exit_ts=_dt(ent_h + 10), exit_px=101.0, initial_risk=1.0,
            pnl_r=r, pnl_pips=r * 10, exit_reason="TP" if r > 0 else "SL", hold_bars=10,
        )

    sim = [
        _st("USD_JPY", "SB", "LONG", 0, 1, 1.5),    # → MATCHED(実も勝ち)
        _st("EUR_USD", "MACD", "SHORT", 5, 6, 1.0),  # → MISSING
    ]
    actual = [
        {"pair": "USD_JPY", "strategy": "SB", "side": "LONG", "entry_time": _dt(1),
         "exit_time": _dt(18), "pnl_jpy": 3000.0, "exit_reason": "TP", "risk_pct": 0.0225},
        {"pair": "GBP_JPY", "strategy": "SB", "side": "LONG", "entry_time": _dt(2),
         "exit_time": _dt(9), "pnl_jpy": -1000.0, "exit_reason": "SL", "risk_pct": 0.0225},  # → EXTRA
    ]
    td = replay.diff_trades(sim, actual, pd.DataFrame())
    assert len(td.matched) == 1
    assert td.matched[0]["outcome_agree"] is True
    assert len(td.missing) == 1 and td.missing[0]["strategy"] == "MACD"
    assert len(td.extra) == 1 and td.extra[0]["pair"] == "GBP_JPY"


def test_run_trades_writes_trade_mode_parity(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", future=True)
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng, future=True)()

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    demo = ohlc_mod.make_demo_ohlc(["USD_JPY", "EUR_USD"], now - timedelta(days=90), now)
    exp = generate_all_signals(demo)
    sims = exits_mod.simulate_trades(demo, exp)
    assert sims, "デモ OHLC でトレードがシミュレートされること"
    # sim の一部を実 trades として投入
    for k, s in enumerate(sims[: max(1, len(sims) // 3)]):
        session.add(Trade(
            position_id=f"p{k}", pair=s.pair, strategy=s.strategy, side=s.side,
            entry_time=s.entry_ts, entry_price=s.entry_px, lot=1000, risk_pct=0.01125,
            exit_time=s.exit_ts, exit_price=s.exit_px,
            pnl_jpy=s.pnl_r * 0.01125 * 300000, exit_reason=s.exit_reason, status="closed",
        ))
    session.commit()

    pr = replay.run_trades(demo, session, "test")
    assert pr.mode == "trade"
    assert 1 <= pr.expected_n <= len(sims)   # 実 trades の期間に絞られる
    assert pr.matched_n >= 1
    got = session.scalars(select(ParityRun).where(ParityRun.mode == "trade")).all()
    assert len(got) == 1


def test_run_writes_parity_run(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'p.db'}", future=True)
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng, future=True)()

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    demo = ohlc_mod.make_demo_ohlc(["USD_JPY"], now - timedelta(days=60), now)
    exp = generate_all_signals(demo)
    # 期待シグナルの一部をそのまま「実運用シグナル」として投入
    for e in exp[: max(1, len(exp) // 2)]:
        session.add(SignalEvent(ts=e.ts.to_pydatetime(), pair=e.pair, strategy=e.strategy,
                                side=e.side, event_type="signal", line_hash=f"h{e.ts}{e.strategy}"))
    session.commit()

    pr = replay.run(demo, session, "test")
    got = session.scalars(select(ParityRun)).all()
    assert len(got) == 1
    assert pr.expected_n > 0
    assert pr.matched_n >= 1
    assert pr.match_rate > 0.0
