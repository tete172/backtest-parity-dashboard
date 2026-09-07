"""バックテスト整合性チェックのテスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import reconciliation
from app.models import Base, EquitySnapshot, SignalEvent, Trade


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'r.db'}", future=True)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine):
    return sessionmaker(bind=engine, future=True)()


def _dt(h: int) -> datetime:
    return datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(hours=h)


def _status(result: dict, key: str) -> str:
    return next(c["status"] for c in result["checks"] if c["key"] == key)


def test_execution_rate_fail_when_many_insufficient(engine, session):
    for i in range(2):
        session.add(SignalEvent(ts=_dt(i), pair="USD_JPY", strategy="SB",
                                event_type="order", result="EXECUTED",
                                position_id=f"e{i}", line_hash=f"h{i}"))
    for i in range(8):
        session.add(SignalEvent(ts=_dt(i), pair="USD_JPY", strategy="SB",
                                event_type="order", result="MARGIN_INSUFFICIENT",
                                line_hash=f"n{i}"))
    session.commit()
    r = reconciliation.reconcile(engine)
    assert _status(r, "execution_rate") == "fail"
    assert r["verdict"] == "fail"


def test_double_execution_detected(engine, session):
    # 同一時刻・同一ペア×手法で EXECUTED が 2 件
    for pid in ("100", "101"):
        session.add(SignalEvent(ts=_dt(0), pair="GBP_USD", strategy="MACD",
                                event_type="order", result="EXECUTED",
                                position_id=pid, line_hash=f"h{pid}"))
    session.commit()
    r = reconciliation.reconcile(engine)
    assert _status(r, "double_exec") == "fail"


def test_one_position_per_pair_violation(engine, session):
    session.add(Trade(position_id="a", pair="USD_JPY", strategy="SB", side="LONG",
                      entry_time=_dt(0), entry_price=147.0, lot=1000,
                      exit_time=_dt(5), status="closed"))
    session.add(Trade(position_id="b", pair="USD_JPY", strategy="SB", side="LONG",
                      entry_time=_dt(2), entry_price=147.5, lot=1000,
                      exit_time=_dt(7), status="closed"))  # a とオープン期間が重なる
    session.commit()
    r = reconciliation.reconcile(engine)
    assert _status(r, "one_per_pair") == "fail"


def test_position_limit_warns_at_code_limit(engine, session):
    # 同時に 6 建玉(コードの max_positions=6 に到達)
    for i in range(6):
        session.add(Trade(position_id=f"p{i}", pair=f"P{i}_JPY", strategy="SB",
                          side="LONG", entry_time=_dt(0), entry_price=100.0,
                          lot=1000, exit_time=_dt(10), status="closed"))
    session.add(EquitySnapshot(ts=_dt(1), balance_jpy=300000, equity_jpy=300000,
                               margin_used_jpy=1, margin_ratio=1.0, open_positions=6))
    session.commit()
    r = reconciliation.reconcile(engine)
    assert _status(r, "position_limit") == "warn"


def test_signal_mix_drift_warns(engine, session):
    # 全部 SB → バックテスト期待比率(SB 約48%)から大きく乖離
    for i in range(50):
        session.add(SignalEvent(ts=_dt(i % 24), pair="USD_JPY", strategy="SB",
                                event_type="signal", line_hash=f"s{i}"))
    session.commit()
    r = reconciliation.reconcile(engine)
    assert _status(r, "signal_mix") == "warn"


def test_clean_data_passes(engine, session):
    # 乖離のない最小データ
    session.add(SignalEvent(ts=_dt(0), pair="USD_JPY", strategy="SB",
                            event_type="order", result="EXECUTED",
                            position_id="x", line_hash="hx"))
    session.add(Trade(position_id="x", pair="USD_JPY", strategy="SB", side="LONG",
                      entry_time=_dt(0), entry_price=147.0, lot=1000, risk_pct=0.0225,
                      exit_time=_dt(3), pnl_jpy=1000.0, status="closed"))
    # 期待比率どおりのシグナル分布
    mix = {"SB": 48, "MACD": 30, "DM_PSAR": 15, "BB_SQ": 8}
    n = 0
    for strat, cnt in mix.items():
        for _ in range(cnt):
            session.add(SignalEvent(ts=_dt(n % 24), pair="USD_JPY", strategy=strat,
                                    event_type="signal", line_hash=f"m{n}"))
            n += 1
    session.commit()
    r = reconciliation.reconcile(engine)
    assert _status(r, "double_exec") == "ok"
    assert _status(r, "one_per_pair") == "ok"
    assert _status(r, "signal_mix") == "ok"
    assert r["verdict"] in ("ok", "warn")  # perf は info、他は ok
