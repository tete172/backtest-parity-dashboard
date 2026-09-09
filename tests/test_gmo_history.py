"""GMO 約定履歴ローダのテスト(ネットワークなし)。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from ingest import gmo_history as gh
from app.models import Base, Trade


@pytest.fixture()
def session(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'g.db'}", future=True)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, future=True)()


def _mk_trade(session, pid, pair="USD_JPY", entry_price=0.0):
    tr = Trade(position_id=pid, pair=pair, strategy="SB", side="LONG",
               entry_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
               entry_price=entry_price, lot=0.0, status="open")
    session.add(tr)
    session.commit()
    return tr


def test_close_execution_fills_pnl_on_matching_trade(session):
    _mk_trade(session, "111")
    execs = [{
        "positionId": "111", "symbol": "USD_JPY", "side": "SELL", "settleType": "CLOSE",
        "price": "148.031", "size": "10000", "lossGain": "4152",
        "timestamp": "2026-06-02T03:15:07.011Z",
    }]
    c = gh.apply_executions(session, execs)
    session.commit()
    assert c["close_matched"] == 1
    tr = session.scalar(select(Trade).where(Trade.position_id == "111"))
    assert tr.status == "closed"
    assert tr.exit_price == pytest.approx(148.031)
    assert tr.pnl_jpy == pytest.approx(4152)
    assert tr.exit_reason == "GMO約定履歴"
    # SQLite は tz を保持しないので値だけ確認
    assert (tr.exit_time.year, tr.exit_time.month, tr.exit_time.day) == (2026, 6, 2)


def test_close_execution_without_trade_creates_closed_trade(session):
    execs = [{
        "positionId": "999", "symbol": "EUR_JPY", "side": "BUY", "settleType": "CLOSE",
        "price": "160.5", "size": "8000", "lossGain": "-1200",
        "timestamp": "2026-07-01T00:00:00Z",
    }]
    c = gh.apply_executions(session, execs)
    session.commit()
    assert c["close_new"] == 1
    tr = session.scalar(select(Trade).where(Trade.position_id == "999"))
    assert tr is not None and tr.status == "closed"
    assert tr.pnl_jpy == pytest.approx(-1200)
    assert tr.side == "SHORT"  # 決済 BUY → エントリーは SELL/SHORT


def test_open_execution_fills_entry_price(session):
    _mk_trade(session, "222", entry_price=0.0)
    c = gh.apply_executions(session, [{
        "positionId": "222", "symbol": "USD_JPY", "side": "BUY", "settleType": "OPEN",
        "price": "147.512", "size": "10000", "timestamp": "2026-06-01T09:01:08Z",
    }])
    session.commit()
    assert c["open_filled"] == 1
    tr = session.scalar(select(Trade).where(Trade.position_id == "222"))
    assert tr.entry_price == pytest.approx(147.512) and tr.status == "open"


def test_japanese_settle_and_side_are_normalized(session):
    """GMO の日本語 CSV(区分=決済 / 売買=買)でも CLOSE 扱いになり損益が入る。"""
    _mk_trade(session, "333")
    c = gh.apply_executions(session, [{
        "positionId": "333", "symbol": "USD_JPY", "side": "買", "settleType": "決済",
        "price": "149.2", "size": "10000", "lossGain": "-880",
        "timestamp": "2026/06/03 12:00:00",
    }])
    session.commit()
    assert c["close_matched"] == 1
    tr = session.scalar(select(Trade).where(Trade.position_id == "333"))
    assert tr.status == "closed" and tr.pnl_jpy == pytest.approx(-880)


def test_parse_csv_english_and_japanese_headers(tmp_path):
    p = tmp_path / "en.csv"
    p.write_text(
        "positionId,symbol,side,settleType,price,size,lossGain,timestamp\n"
        "111,USD_JPY,SELL,CLOSE,148.03,10000,4152,2026-06-02T03:15:07Z\n",
        encoding="utf-8",
    )
    rows = gh.parse_csv(str(p))
    assert rows[0]["positionId"] == "111" and rows[0]["lossGain"] == "4152"

    p2 = tmp_path / "jp.csv"
    p2.write_text(
        "日時,通貨ペア,売買,決済区分,約定Rate,約定数量,決済損益,ポジションID\n"
        "2026/06/02 03:15:07,USD_JPY,売,決済,148.031,10000,4152,111\n",
        encoding="utf-8-sig",
    )
    rows = gh.parse_csv(str(p2))
    assert rows[0]["positionId"] == "111"
    assert rows[0]["settleType"] == "決済" and rows[0]["price"] == "148.031"
