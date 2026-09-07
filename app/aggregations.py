"""集計層(pandas)。

DB 方言に依存しないよう、生の行を DataFrame に読み出してから Python 側で集計する。
各 public 関数は JSON シリアライズ可能な dict / list を返し、main.py 側で cache.get_or_set に包む。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import Engine

from .config import get_settings
from .db import engine as default_engine

# バックテストで BE(損益分岐)がマイナス寄りだった要監視ペア
WATCH_PAIRS = {"AUD_JPY", "AUD_USD", "CAD_JPY"}

# 戦略の優先順位(表示順に使う)
STRATEGY_ORDER = ["SB", "MACD", "DM_PSAR", "BB_SQ"]


def _read(table: str, eng: Engine) -> pd.DataFrame:
    try:
        return pd.read_sql_table(table, eng)
    except ValueError:
        # テーブル未作成(初回起動直後など)
        return pd.DataFrame()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_dt(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


# --------------------------------------------------------------------------- #
# サマリー(KPI + ボット死活 + 口座状況 + キャッシュ backend)
# --------------------------------------------------------------------------- #
def summary(eng: Engine | None = None) -> dict[str, Any]:
    eng = eng or default_engine
    settings = get_settings()
    from . import cache

    trades = _read("trades", eng)
    hb = _read("bot_heartbeats", eng)
    eq = _read("equity_snapshots", eng)
    meta = _read("app_meta", eng)
    data_mode = "unknown"
    if not meta.empty and "key" in meta.columns:
        row = meta[meta["key"] == "data_mode"]
        if not row.empty:
            data_mode = str(row["value"].iloc[0])

    closed = trades[trades["status"] == "closed"] if not trades.empty else pd.DataFrame()

    wins = losses = 0
    gross_profit = gross_loss = net_pnl = 0.0
    win_rate = profit_factor = expectancy = 0.0
    avg_win = avg_loss = 0.0
    if not closed.empty:
        pnl = closed["pnl_jpy"].fillna(0.0)
        wins = int((pnl > 0).sum())
        losses = int((pnl <= 0).sum())
        gross_profit = float(pnl[pnl > 0].sum())
        gross_loss = float(-pnl[pnl <= 0].sum())
        net_pnl = float(pnl.sum())
        win_rate = wins / len(pnl) if len(pnl) else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss else float("inf")
        expectancy = net_pnl / len(pnl) if len(pnl) else 0.0
        avg_win = float(pnl[pnl > 0].mean()) if wins else 0.0
        avg_loss = float(pnl[pnl <= 0].mean()) if losses else 0.0

    open_positions = (
        int((trades["status"] == "open").sum()) if not trades.empty else 0
    )

    # --- 最大ドローダウン(日次エクイティから)---
    max_dd_pct = 0.0
    if not eq.empty:
        e = eq.copy()
        e["ts"] = _to_dt(e["ts"])
        e = e.sort_values("ts")
        daily = e.set_index("ts")["equity_jpy"].resample("1D").last().dropna()
        if not daily.empty:
            peak = daily.cummax()
            dd = daily / peak - 1.0
            max_dd_pct = float(dd.min() * 100.0)

    # --- 口座状況(最新スナップショット)---
    account = None
    if not eq.empty:
        e = eq.copy()
        e["ts"] = _to_dt(e["ts"])
        last = e.sort_values("ts").iloc[-1]
        account = {
            "ts": last["ts"].isoformat(),
            "balance_jpy": float(last["balance_jpy"]),
            "equity_jpy": float(last["equity_jpy"]),
            "margin_used_jpy": float(last["margin_used_jpy"]),
            "margin_ratio": (
                None if pd.isna(last["margin_ratio"]) else float(last["margin_ratio"])
            ),
            "open_positions": int(last["open_positions"]),
        }

    # --- ボット死活(hourly サイクル前提)---
    bot = {"status": "unknown", "last_cycle_at": None, "minutes_since": None}
    if not hb.empty:
        h = hb.copy()
        h["ts"] = _to_dt(h["ts"])
        last_ts = h["ts"].max()
        minutes_since = (_now() - last_ts.to_pydatetime()).total_seconds() / 60.0
        if minutes_since <= settings.bot_cycle_interval_minutes * 1.5:
            status = "healthy"
        elif minutes_since <= settings.bot_stale_after_minutes:
            status = "stale"
        else:
            status = "down"
        bot = {
            "status": status,
            "last_cycle_at": last_ts.isoformat(),
            "minutes_since": round(minutes_since, 1),
        }

    return {
        "generated_at": _now().isoformat(),
        "data_mode": data_mode,
        "cache_backend": cache.backend(),
        "kpi": {
            "total_trades": int(len(closed)),
            "open_positions": open_positions,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "gross_profit_jpy": round(gross_profit, 0),
            "gross_loss_jpy": round(gross_loss, 0),
            "net_pnl_jpy": round(net_pnl, 0),
            "profit_factor": (
                None if profit_factor == float("inf") else round(profit_factor, 2)
            ),
            "expectancy_jpy": round(expectancy, 0),
            "avg_win_jpy": round(avg_win, 0),
            "avg_loss_jpy": round(avg_loss, 0),
            "max_drawdown_pct": round(max_dd_pct, 1),
        },
        "account": account,
        "bot": bot,
    }


# --------------------------------------------------------------------------- #
# エクイティカーブ(日次)+ 実現損益の累積
# --------------------------------------------------------------------------- #
def equity_curve(eng: Engine | None = None) -> list[dict[str, Any]]:
    eng = eng or default_engine
    eq = _read("equity_snapshots", eng)
    trades = _read("trades", eng)
    if eq.empty:
        return []

    eq = eq.copy()
    eq["ts"] = _to_dt(eq["ts"])
    daily_eq = (
        eq.sort_values("ts").set_index("ts")[["equity_jpy", "balance_jpy"]]
        .resample("1D")
        .last()
        .dropna(how="all")
    )

    cum_pnl = pd.Series(dtype="float64")
    if not trades.empty:
        closed = trades[trades["status"] == "closed"].copy()
        if not closed.empty:
            closed["exit_time"] = _to_dt(closed["exit_time"])
            by_day = (
                closed.dropna(subset=["exit_time"])
                .set_index("exit_time")["pnl_jpy"]
                .fillna(0.0)
                .resample("1D")
                .sum()
            )
            cum_pnl = by_day.cumsum()

    out = []
    for day, row in daily_eq.iterrows():
        out.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "equity_jpy": None if pd.isna(row["equity_jpy"]) else round(float(row["equity_jpy"]), 0),
                "balance_jpy": None if pd.isna(row["balance_jpy"]) else round(float(row["balance_jpy"]), 0),
                "cum_realized_pnl_jpy": (
                    round(float(cum_pnl.get(day, cum_pnl.iloc[-1] if not cum_pnl.empty else 0.0)), 0)
                ),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 日次損益
# --------------------------------------------------------------------------- #
def daily_pnl(eng: Engine | None = None) -> list[dict[str, Any]]:
    eng = eng or default_engine
    trades = _read("trades", eng)
    if trades.empty:
        return []
    closed = trades[trades["status"] == "closed"].copy()
    if closed.empty:
        return []
    closed["exit_time"] = _to_dt(closed["exit_time"])
    by_day = (
        closed.dropna(subset=["exit_time"])
        .set_index("exit_time")["pnl_jpy"]
        .fillna(0.0)
        .resample("1D")
        .sum()
    )
    return [
        {"date": d.strftime("%Y-%m-%d"), "pnl_jpy": round(float(v), 0)}
        for d, v in by_day.items()
    ]


# --------------------------------------------------------------------------- #
# 手法別 / ペア別の成績
# --------------------------------------------------------------------------- #
def _group_stats(closed: pd.DataFrame, key: str) -> list[dict[str, Any]]:
    rows = []
    for name, grp in closed.groupby(key):
        pnl = grp["pnl_jpy"].fillna(0.0)
        wins = int((pnl > 0).sum())
        n = len(pnl)
        gross_profit = float(pnl[pnl > 0].sum())
        gross_loss = float(-pnl[pnl <= 0].sum())
        rows.append(
            {
                key: name,
                "trades": n,
                "win_rate": round(wins / n, 4) if n else 0.0,
                "net_pnl_jpy": round(float(pnl.sum()), 0),
                "expectancy_jpy": round(float(pnl.sum()) / n, 0) if n else 0.0,
                "profit_factor": (
                    round(gross_profit / gross_loss, 2) if gross_loss else None
                ),
            }
        )
    return rows


def by_strategy(eng: Engine | None = None) -> list[dict[str, Any]]:
    eng = eng or default_engine
    trades = _read("trades", eng)
    if trades.empty:
        return []
    closed = trades[trades["status"] == "closed"]
    if closed.empty:
        return []
    rows = _group_stats(closed, "strategy")
    order = {s: i for i, s in enumerate(STRATEGY_ORDER)}
    rows.sort(key=lambda r: order.get(r["strategy"], 99))
    return rows


def by_pair(eng: Engine | None = None) -> list[dict[str, Any]]:
    eng = eng or default_engine
    trades = _read("trades", eng)
    if trades.empty:
        return []
    closed = trades[trades["status"] == "closed"]
    if closed.empty:
        return []
    rows = _group_stats(closed, "pair")
    for r in rows:
        r["watch"] = r["pair"] in WATCH_PAIRS
    rows.sort(key=lambda r: r["net_pnl_jpy"])
    return rows


# --------------------------------------------------------------------------- #
# シグナル実行率(発注できた vs 証拠金不足で失注)
# --------------------------------------------------------------------------- #
def signal_stats(eng: Engine | None = None) -> dict[str, Any]:
    eng = eng or default_engine
    ev = _read("signal_events", eng)
    if ev.empty:
        return {"by_result": [], "execution_rate": None, "by_strategy": []}

    orders = ev[ev["event_type"] == "order"].copy()
    if orders.empty:
        return {"by_result": [], "execution_rate": None, "by_strategy": []}

    by_result = (
        orders.groupby("result").size().sort_values(ascending=False)
        .rename("count").reset_index().to_dict("records")
    )
    executed = int((orders["result"] == "EXECUTED").sum())
    insufficient = int((orders["result"] == "MARGIN_INSUFFICIENT").sum())
    denom = executed + insufficient
    execution_rate = round(executed / denom, 4) if denom else None

    by_strat = (
        orders.assign(is_exec=lambda d: (d["result"] == "EXECUTED").astype(int))
        .groupby("strategy")
        .agg(orders=("result", "size"), executed=("is_exec", "sum"))
        .reset_index()
    )
    by_strat["execution_rate"] = (
        (by_strat["executed"] / by_strat["orders"]).round(4)
    )
    order_map = {s: i for i, s in enumerate(STRATEGY_ORDER)}
    strat_rows = by_strat.to_dict("records")
    strat_rows.sort(key=lambda r: order_map.get(r["strategy"], 99))

    return {
        "by_result": [{"result": r["result"], "count": int(r["count"])} for r in by_result],
        "executed": executed,
        "margin_insufficient": insufficient,
        "execution_rate": execution_rate,
        "by_strategy": [
            {
                "strategy": r["strategy"],
                "orders": int(r["orders"]),
                "executed": int(r["executed"]),
                "execution_rate": float(r["execution_rate"]),
            }
            for r in strat_rows
        ],
    }


# --------------------------------------------------------------------------- #
# 直近イベント / オープンポジション
# --------------------------------------------------------------------------- #
def recent_events(limit: int = 50, eng: Engine | None = None) -> list[dict[str, Any]]:
    eng = eng or default_engine
    ev = _read("signal_events", eng)
    if ev.empty:
        return []
    ev = ev.copy()
    ev["ts"] = _to_dt(ev["ts"])
    ev = ev.sort_values("ts", ascending=False).head(limit)
    cols = [
        "ts", "pair", "strategy", "side", "event_type", "result",
        "position_id", "price", "pnl_jpy", "reason", "detail",
    ]
    out = []
    for _, r in ev[cols].iterrows():
        out.append(
            {
                "ts": r["ts"].isoformat() if pd.notna(r["ts"]) else None,
                "pair": r["pair"],
                "strategy": r["strategy"],
                "side": None if pd.isna(r["side"]) else r["side"],
                "event_type": r["event_type"],
                "result": None if pd.isna(r["result"]) else r["result"],
                "position_id": None if pd.isna(r["position_id"]) else r["position_id"],
                "price": None if pd.isna(r["price"]) else float(r["price"]),
                "pnl_jpy": None if pd.isna(r["pnl_jpy"]) else float(r["pnl_jpy"]),
                "reason": None if pd.isna(r["reason"]) else r["reason"],
                "detail": None if pd.isna(r["detail"]) else r["detail"],
            }
        )
    return out


def open_positions(eng: Engine | None = None) -> list[dict[str, Any]]:
    eng = eng or default_engine
    trades = _read("trades", eng)
    if trades.empty:
        return []
    op = trades[trades["status"] == "open"].copy()
    if op.empty:
        return []
    op["entry_time"] = _to_dt(op["entry_time"])
    op = op.sort_values("entry_time")
    return [
        {
            "position_id": r["position_id"],
            "pair": r["pair"],
            "strategy": r["strategy"],
            "side": r["side"],
            "entry_time": r["entry_time"].isoformat(),
            "entry_price": float(r["entry_price"]),
            "lot": float(r["lot"]),
            "risk_pct": None if pd.isna(r["risk_pct"]) else float(r["risk_pct"]),
        }
        for _, r in op.iterrows()
    ]
