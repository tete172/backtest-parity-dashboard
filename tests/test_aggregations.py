"""集計層のテスト。小さな確定データを一時 SQLite に入れて数値を検証する。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import aggregations
from app.models import AppMeta, Base, BotHeartbeat, EquitySnapshot, SignalEvent, Trade


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", future=True)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine):
    return sessionmaker(bind=engine, future=True)()


def _dt(h: int) -> datetime:
    return datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(hours=h)


def test_summary_kpi_math(engine, session):
    # 3 勝 2 敗 / gross_profit=600, gross_loss=200 -> PF=3.0, net=+400
    pnls = [100.0, 200.0, 300.0, -50.0, -150.0]
    for i, p in enumerate(pnls):
        session.add(
            Trade(
                position_id=f"p{i}", pair="USD_JPY", strategy="SB", side="LONG",
                entry_time=_dt(i), entry_price=147.0, lot=1000, risk_pct=0.0225,
                exit_time=_dt(i + 1), exit_price=147.5, pnl_jpy=p,
                exit_reason="TP" if p > 0 else "SL", status="closed",
            )
        )
    session.add(
        Trade(
            position_id="open1", pair="EUR_USD", strategy="MACD", side="SHORT",
            entry_time=_dt(10), entry_price=1.1, lot=2000, status="open",
        )
    )
    session.add(BotHeartbeat(ts=_dt(11), cycle_id="c1", pairs_checked=12))
    session.add(
        EquitySnapshot(
            ts=_dt(11), balance_jpy=300400, equity_jpy=300400,
            margin_used_jpy=8000, margin_ratio=3755.0, open_positions=1,
        )
    )
    session.commit()

    s = aggregations.summary(engine)
    k = s["kpi"]
    assert k["total_trades"] == 5
    assert k["wins"] == 3 and k["losses"] == 2
    assert k["win_rate"] == pytest.approx(0.6)
    assert k["net_pnl_jpy"] == 400
    assert k["profit_factor"] == pytest.approx(3.0)
    assert k["open_positions"] == 1
    assert s["account"]["margin_ratio"] == 3755.0


def test_signal_execution_rate(engine, session):
    # order: EXECUTED x3, MARGIN_INSUFFICIENT x7 -> 実行率 0.3
    for i in range(3):
        session.add(
            SignalEvent(
                ts=_dt(i), pair="USD_JPY", strategy="SB", side="LONG",
                event_type="order", result="EXECUTED", position_id=f"e{i}",
                line_hash=f"h-ok-{i}",
            )
        )
    for i in range(7):
        session.add(
            SignalEvent(
                ts=_dt(i), pair="USD_JPY", strategy="SB", side="LONG",
                event_type="order", result="MARGIN_INSUFFICIENT",
                line_hash=f"h-ng-{i}",
            )
        )
    session.commit()

    st = aggregations.signal_stats(engine)
    assert st["executed"] == 3
    assert st["margin_insufficient"] == 7
    assert st["execution_rate"] == pytest.approx(0.3)


def test_by_pair_flags_watch_pairs(engine, session):
    session.add(
        Trade(
            position_id="w1", pair="AUD_JPY", strategy="DM_PSAR", side="LONG",
            entry_time=_dt(0), entry_price=95.0, lot=1000,
            exit_time=_dt(1), exit_price=94.0, pnl_jpy=-1200.0,
            exit_reason="SL", status="closed",
        )
    )
    session.add(
        Trade(
            position_id="n1", pair="USD_JPY", strategy="SB", side="LONG",
            entry_time=_dt(0), entry_price=147.0, lot=1000,
            exit_time=_dt(1), exit_price=148.0, pnl_jpy=3000.0,
            exit_reason="TP", status="closed",
        )
    )
    session.commit()

    rows = {r["pair"]: r for r in aggregations.by_pair(engine)}
    assert rows["AUD_JPY"]["watch"] is True
    assert rows["USD_JPY"]["watch"] is False
    # 純損益の昇順で並ぶ(負けペアが先頭)
    assert aggregations.by_pair(engine)[0]["pair"] == "AUD_JPY"


def test_data_mode_from_app_meta(engine, session):
    assert aggregations.summary(engine)["data_mode"] == "unknown"
    session.add(AppMeta(key="data_mode", value="demo (合成データ)"))
    session.commit()
    assert aggregations.summary(engine)["data_mode"].startswith("demo")


def test_empty_db_is_safe(engine):
    s = aggregations.summary(engine)
    assert s["kpi"]["total_trades"] == 0
    assert s["data_mode"] == "unknown"
    assert aggregations.equity_curve(engine) == []
    assert aggregations.by_strategy(engine) == []
    assert aggregations.recent_events(10, engine) == []
