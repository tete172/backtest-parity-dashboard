"""DB モデル。

fxbot.log を構造化して 4 テーブルに落とす:

- bot_heartbeats  : メインチェック 1 サイクル = 1 行(死活監視の元データ)
- equity_snapshots: 口座状況(有効証拠金・維持率・建玉数)のスナップショット
- signal_events   : シグナル検出 / 発注結果 / 決済の生イベント(発注失敗も含む)
- trades          : signal_events から導出した「1 ポジションの entry〜exit」1 行
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AppMeta(Base):
    """key/value のメタ情報。`data_mode` = demo(合成デモデータ)/ live(実ログ取り込み済み)。"""

    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


class BotHeartbeat(Base):
    __tablename__ = "bot_heartbeats"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cycle_id: Mapped[str] = mapped_column(String(32), unique=True)
    pairs_checked: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True, index=True)
    balance_jpy: Mapped[float] = mapped_column(Float)
    equity_jpy: Mapped[float] = mapped_column(Float)
    margin_used_jpy: Mapped[float] = mapped_column(Float, default=0.0)
    margin_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)


class SignalEvent(Base):
    __tablename__ = "signal_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    pair: Mapped[str] = mapped_column(String(16), index=True)
    strategy: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # signal(検出) / order(発注結果) / exit(決済)
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    # EXECUTED / MARGIN_INSUFFICIENT / SKIPPED / CLOSED / ...
    result: Mapped[str | None] = mapped_column(String(24), nullable=True)
    position_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    lot: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_jpy: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 同じログ行を 2 回取り込んでも重複しないための冪等キー(raw 行の SHA1)
    line_hash: Mapped[str] = mapped_column(String(40), unique=True)


Index("ix_signal_events_type_result", SignalEvent.event_type, SignalEvent.result)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    pair: Mapped[str] = mapped_column(String(16), index=True)
    strategy: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    entry_price: Mapped[float] = mapped_column(Float)
    lot: Mapped[float] = mapped_column(Float)
    risk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_jpy: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="open", index=True)


class ParityRun(Base):
    """バックテスト再現(トレード単位照合)の 1 回ぶんの結果サマリ。

    `backtest/replay.py` が OHLC を戦略ロジックに通して「期待シグナル」を再現し、
    実運用の signal_events と突き合わせた結果を格納する。詳細は detail_json。
    """

    __tablename__ = "parity_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # signal = entry シグナルの照合 / trade = exit まで再現したトレード・損益の照合
    mode: Mapped[str] = mapped_column(String(8), default="signal", index=True)
    ohlc_source: Mapped[str] = mapped_column(String(120))
    bars: Mapped[int] = mapped_column(Integer, default=0)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_n: Mapped[int] = mapped_column(Integer, default=0)
    actual_n: Mapped[int] = mapped_column(Integer, default=0)
    matched_n: Mapped[int] = mapped_column(Integer, default=0)
    missing_n: Mapped[int] = mapped_column(Integer, default=0)   # 期待あり・実運用なし
    extra_n: Mapped[int] = mapped_column(Integer, default=0)     # 実運用あり・期待なし
    mismatch_n: Mapped[int] = mapped_column(Integer, default=0)  # 時刻/ペア一致・方向違い
    match_rate: Mapped[float] = mapped_column(Float, default=0.0)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
