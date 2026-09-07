"""バックテスト整合性チェック(Reconciliation)。

このダッシュボードの中心機能。資産推移を眺めるためではなく、
「実運用がバックテストの前提(`app/baseline.py`)どおりに取引できているか」を
機械的に突き合わせて、乖離を ok / warn / fail で提示する。

背景: 実運用の 3 ヶ月ログ分析(2026-09-05)で、
  - シグナルの過半数が証拠金不足で失注(バックテストは全約定前提)
  - ボットプロセスが二重起動して実質 2 倍のリスク
  - CONFIG['max_positions']=6 がドキュメント方針「上限なし」と不一致
といった「資産推移だけ見ていては気づけない」乖離が見つかった。それを常時監視する。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import json

import pandas as pd
from sqlalchemy import Engine

from . import baseline
from .db import engine as default_engine

_OK, _WARN, _FAIL, _INFO = "ok", "warn", "fail", "info"


def _read(table: str, eng: Engine) -> pd.DataFrame:
    try:
        return pd.read_sql_table(table, eng)
    except ValueError:
        return pd.DataFrame()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check(key, label, status, expected, actual, note=""):
    return {
        "key": key,
        "label": label,
        "status": status,
        "expected": expected,
        "actual": actual,
        "note": note,
    }


# --------------------------------------------------------------------------- #
# 個別チェック
# --------------------------------------------------------------------------- #
def _check_execution_rate(ev: pd.DataFrame) -> dict:
    orders = ev[ev["event_type"] == "order"] if not ev.empty else pd.DataFrame()
    if orders.empty:
        return _check("execution_rate", "シグナル→約定の実行率", _INFO,
                      "≈100%(バックテストは全シグナル約定前提)", "データなし")
    executed = int((orders["result"] == "EXECUTED").sum())
    insufficient = int((orders["result"] == "MARGIN_INSUFFICIENT").sum())
    skipped = int((orders["result"] == "SKIPPED").sum())
    denom = executed + insufficient + skipped
    rate = executed / denom if denom else 0.0
    if rate < baseline.EXECUTION_RATE_FAIL:
        status = _FAIL
    elif rate < baseline.EXECUTION_RATE_WARN:
        status = _WARN
    else:
        status = _OK
    lost = insufficient + skipped
    return _check(
        "execution_rate", "シグナル→約定の実行率", status,
        f"≥ {baseline.EXECUTION_RATE_WARN:.0%}",
        f"{rate:.1%}",
        f"発注 {denom} 件中 {lost} 件が未約定"
        f"(証拠金不足 {insufficient} / スキップ {skipped})。"
        f"バックテストは全約定前提のため、この分だけ想定取引を取りこぼしている。",
    )


def _check_signal_mix(ev: pd.DataFrame) -> dict:
    sig = ev[ev["event_type"] == "signal"] if not ev.empty else pd.DataFrame()
    if sig.empty:
        sig = ev[ev["event_type"] == "order"] if not ev.empty else pd.DataFrame()
    if sig.empty:
        return _check("signal_mix", "手法別シグナル比率のドリフト", _INFO,
                      baseline.EXPECTED_SIGNAL_MIX, "データなし")
    counts = sig.groupby("strategy").size()
    total = int(counts.sum())
    observed = {k: round(int(counts.get(k, 0)) / total, 4) for k in baseline.STRATEGY_PRIORITY}
    drifts = {
        k: round(observed[k] - baseline.EXPECTED_SIGNAL_MIX.get(k, 0.0), 4)
        for k in baseline.STRATEGY_PRIORITY
    }
    worst = max(drifts, key=lambda k: abs(drifts[k]))
    status = _WARN if abs(drifts[worst]) > baseline.SIGNAL_MIX_TOLERANCE else _OK
    detail = " / ".join(
        f"{k}: 実測 {observed[k]:.0%} vs 期待 {baseline.EXPECTED_SIGNAL_MIX.get(k, 0):.0%}"
        f"({drifts[k]:+.0%})"
        for k in baseline.STRATEGY_PRIORITY
    )
    return _check(
        "signal_mix", "手法別シグナル比率のドリフト", status,
        {k: f"{v:.0%}" for k, v in baseline.EXPECTED_SIGNAL_MIX.items()},
        {k: f"{v:.0%}" for k, v in observed.items()},
        detail + f"。最大乖離: {worst} {drifts[worst]:+.0%}",
    )


def _check_risk_pct(trades: pd.DataFrame) -> dict:
    closed_or_open = trades if not trades.empty else pd.DataFrame()
    if closed_or_open.empty or closed_or_open["risk_pct"].dropna().empty:
        return _check("risk_pct", "手法別リスク% の一致", _INFO,
                      baseline.STRATEGY_RISK_PCT, "risk_pct 未記録")
    mismatches = []
    actual = {}
    for strat, grp in closed_or_open.groupby("strategy"):
        vals = sorted({round(float(x), 5) for x in grp["risk_pct"].dropna().unique()})
        actual[strat] = vals
        exp = baseline.STRATEGY_RISK_PCT.get(strat)
        if exp is None:
            continue
        if vals != [round(exp, 5)]:
            mismatches.append(f"{strat}: 実測 {vals} vs 期待 {exp}")
    status = _WARN if mismatches else _OK
    return _check(
        "risk_pct", "手法別リスク% の一致", status,
        baseline.STRATEGY_RISK_PCT, actual,
        "; ".join(mismatches) if mismatches else "全手法で一致",
    )


def _sweep_max_concurrent(trades: pd.DataFrame) -> tuple[int, int]:
    """トレードの [entry, exit] を走査して最大同時建玉数とその到達回数を返す。"""
    if trades.empty:
        return 0, 0
    now = _now()
    events: list[tuple[datetime, int]] = []
    for _, r in trades.iterrows():
        entry = pd.to_datetime(r["entry_time"], utc=True)
        exit_ = pd.to_datetime(r["exit_time"], utc=True) if pd.notna(r["exit_time"]) else pd.Timestamp(now)
        events.append((entry, 1))
        events.append((exit_, -1))
    events.sort(key=lambda e: (e[0], -e[1]))
    cur = mx = hit = 0
    for _, delta in events:
        cur += delta
        if cur > mx:
            mx, hit = cur, 1
        elif cur == mx and delta == 1:
            hit += 1
    return mx, hit


def _check_position_limit(trades: pd.DataFrame, eq: pd.DataFrame) -> dict:
    mx, hit = _sweep_max_concurrent(trades)
    snap_max = int(eq["open_positions"].max()) if not eq.empty else 0
    observed_max = max(mx, snap_max)
    limit = baseline.POSITION_LIMIT_IN_CODE
    if observed_max >= limit:
        status = _WARN
        note = (
            f"最大同時建玉数が {observed_max}。本番コードの CONFIG['max_positions']={limit} に"
            f"達しており(到達 {hit} 回)、ドキュメント方針「上限なし」と不一致(2026-09-05 に発見済みの既知事項)。"
        )
    else:
        status = _OK
        note = f"最大同時建玉数 {observed_max}。コードの上限 {limit} 未満。"
    return _check(
        "position_limit", "同時建玉数の上限", status,
        "上限なし(方針) / コードは 6",
        str(observed_max), note,
    )


def _check_one_per_pair(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return _check("one_per_pair", "1 ペア 1 ポジション制約", _INFO,
                      "重なりゼロ", "データなし")
    now = pd.Timestamp(_now())
    df = trades.copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True).fillna(now)
    violations = []
    for pair, grp in df.sort_values("entry_time").groupby("pair"):
        rows = grp[["entry_time", "exit_time", "position_id"]].to_dict("records")
        for i in range(1, len(rows)):
            if rows[i]["entry_time"] < rows[i - 1]["exit_time"]:
                violations.append((pair, rows[i - 1]["position_id"], rows[i]["position_id"]))
    status = _FAIL if violations else _OK
    ex = "; ".join(f"{p}: {a}↔{b}" for p, a, b in violations[:3])
    return _check(
        "one_per_pair", "1 ペア 1 ポジション制約", status,
        "同一ペアで建玉期間の重なりゼロ",
        f"{len(violations)} 件の重なり",
        (f"例) {ex}。同一ペアで同時に複数ポジションを保有しており、"
         f"バックテスト前提(1 ペア 1 ポジション)から外れている。" if violations
         else "重なりなし"),
    )


def _check_double_execution(ev: pd.DataFrame) -> dict:
    if ev.empty:
        return _check("double_exec", "二重発注 / 二重稼働の疑い", _INFO,
                      "重複ゼロ", "データなし")
    ex = ev[(ev["event_type"] == "order") & (ev["result"] == "EXECUTED")].copy()
    if ex.empty:
        return _check("double_exec", "二重発注 / 二重稼働の疑い", _INFO,
                      "重複ゼロ", "約定データなし")
    ex["ts"] = pd.to_datetime(ex["ts"], utc=True)
    ex["bucket"] = ex["ts"].dt.floor("h")
    dup = ex.groupby(["bucket", "pair", "strategy"]).size()
    dup = dup[dup >= 2]
    status = _FAIL if len(dup) else _OK
    examples = "; ".join(
        f"{b:%Y-%m-%d %H:%M} {p}/{s} x{int(n)}"
        for (b, p, s), n in list(dup.items())[:3]
    )
    return _check(
        "double_exec", "二重発注 / 二重稼働の疑い", status,
        "同一サイクル・同一(ペア×手法)の約定は 1 件",
        f"{len(dup)} 件の重複約定",
        (f"例) {examples}。1 ペア 1 ポジション・優先順位ロジック上、"
         f"同一サイクルで同じ手法×ペアが複数約定するのは異常。プロセス二重起動を疑う。" if len(dup)
         else "重複なし"),
    )


def _latest_parity(eng: Engine, mode: str):
    runs = _read("parity_runs", eng)
    if runs.empty:
        return None
    if "mode" in runs.columns:
        runs = runs[runs["mode"] == mode]
    elif mode != "signal":
        return None
    if runs.empty:
        return None
    runs = runs.copy()
    runs["ran_at"] = pd.to_datetime(runs["ran_at"], utc=True)
    return runs.sort_values("ran_at").iloc[-1]


def _check_backtest_parity(eng: Engine) -> dict:
    """entry シグナル単位の照合(`replay.run` / mode='signal')。"""
    r = _latest_parity(eng, "signal")
    if r is None:
        return _check(
            "backtest_parity", "バックテスト再現(シグナル単位)", _INFO,
            "一致率 ≥ 90%", "リプレイ未実行",
            "`python -m backtest.replay --yfinance --start <運用開始> --end <now>` で "
            "バックテスト戦略が出したはずの entry シグナルと実運用シグナルを 1 件ずつ突合する。",
        )
    rate = float(r["match_rate"])
    exp_n = int(r["expected_n"])
    off = int(r["missing_n"]) + int(r["extra_n"]) + int(r["mismatch_n"])
    off_ratio = off / exp_n if exp_n else 0.0
    status = _OK if (rate >= 0.90 and off_ratio <= 0.15) else _WARN if rate >= 0.75 else _FAIL
    return _check(
        "backtest_parity", "バックテスト再現(シグナル単位)", status,
        "一致率 ≥ 90%", f"一致率 {rate:.1%}",
        (f"期待 {exp_n} 件に対し MATCHED {int(r['matched_n'])} / "
         f"MISSING {int(r['missing_n'])}(取り逃し) / EXTRA {int(r['extra_n'])}(想定外の発注) / "
         f"MISMATCH {int(r['mismatch_n'])}(方向逆)。OHLC={r['ohlc_source']}、{r['ran_at']:%Y-%m-%d %H:%M}。"),
    )


def _check_trade_parity(eng: Engine) -> dict:
    """exit まで再現したトレード・損益単位の照合(`replay.run_trades` / mode='trade')。"""
    r = _latest_parity(eng, "trade")
    if r is None:
        return _check(
            "trade_parity", "バックテスト再現(トレード・損益単位)", _INFO,
            "勝敗一致率 ≥ 80%", "リプレイ未実行",
            "`python -m backtest.replay --yfinance --mode trade ...` で、エグジット(SL/TP/トレール)を"
            "再現した「バックテストなら成立したトレード」と実 trades を突合し、勝敗・決済理由・保有時間を比較する。",
        )
    rate = float(r["match_rate"])  # マッチしたトレードのうち勝敗一致の割合
    sim_n = int(r["expected_n"])
    status = _OK if rate >= 0.80 else _WARN if rate >= 0.6 else _FAIL
    detail = {}
    try:
        detail = json.loads(r["detail_json"]) if r.get("detail_json") else {}
    except (TypeError, ValueError):
        pass
    agg = detail.get("aggregate", {})
    cov = agg.get("coverage")
    cov_s = f"{cov:.0%}" if isinstance(cov, (int, float)) else "-"
    return _check(
        "trade_parity", "バックテスト再現(トレード・損益単位)", status,
        "勝敗一致率 ≥ 80%(マッチ分)", f"勝敗一致率 {rate:.1%}",
        (f"バックテスト想定 {sim_n} トレード。うち実際に取れたのは {int(r['matched_n'])} 件"
         f"(カバレッジ {cov_s}、MISSING {int(r['missing_n'])} = 証拠金不足/max_positions 等で見送り)。"
         f"マッチ分の勝敗一致 {rate:.0%}・不一致 {int(r['mismatch_n'])}、決済理由一致率 "
         f"{agg.get('reason_agree_rate')}、平均R sim={agg.get('avg_r_sim')} vs 実={agg.get('avg_r_actual_matched')}。"
         f"EXTRA {int(r['extra_n'])}(バックテストにない実トレード)。エグジットは近似モデル(→ docs/PARITY.md)。"),
    )


def _perf_vs_backtest(trades: pd.DataFrame, eq: pd.DataFrame) -> dict:
    closed = trades[trades["status"] == "closed"] if not trades.empty else pd.DataFrame()
    n = len(closed)
    live = {"trades": n}
    if n:
        pnl = closed["pnl_jpy"].fillna(0.0)
        gp = float(pnl[pnl > 0].sum())
        gl = float(-pnl[pnl <= 0].sum())
        live["win_rate"] = f"{(pnl > 0).mean():.1%}"
        live["profit_factor"] = round(gp / gl, 2) if gl else None
        live["net_pnl_jpy"] = round(float(pnl.sum()), 0)
    if not eq.empty:
        e = eq.copy()
        e["ts"] = pd.to_datetime(e["ts"], utc=True)
        e = e.sort_values("ts")
        days = max((e["ts"].iloc[-1] - e["ts"].iloc[0]).days, 1)
        first, last = float(e["equity_jpy"].iloc[0]), float(e["equity_jpy"].iloc[-1])
        if first > 0:
            # 短期間を年率換算すると誇張になるので、実測期間のリターンを出す。
            live["period_return_pct"] = round((last / first - 1) * 100, 1)
            if days >= 365:
                live["annualized_return_pct"] = round(((last / first) ** (365 / days) - 1) * 100, 1)
        daily = e.set_index("ts")["equity_jpy"].resample("1D").last().dropna()
        if not daily.empty:
            dd = (daily / daily.cummax() - 1.0).min()
            live["max_drawdown_pct"] = round(float(dd) * 100, 1)
        live["observed_days"] = days
    status = _INFO
    note = (
        f"運用 {live.get('observed_days', 0)} 日 / {n} 取引。"
        f"{'判定にはサンプル不足(参考値)' if n < baseline.MIN_TRADES_FOR_PERF_JUDGEMENT else '目安として比較可能'}。"
    )
    return _check(
        "performance", "成績 vs バックテスト(参考)", status,
        {"CAGR": f"{baseline.BACKTEST_CAGR_PCT}%", "MaxDD": f"{baseline.BACKTEST_MAX_DD_PCT}%"},
        live, note,
    )


# --------------------------------------------------------------------------- #
# エントリポイント
# --------------------------------------------------------------------------- #
def reconcile(eng: Engine | None = None) -> dict[str, Any]:
    eng = eng or default_engine
    ev = _read("signal_events", eng)
    trades = _read("trades", eng)
    eq = _read("equity_snapshots", eng)

    checks = [
        _check_backtest_parity(eng),
        _check_trade_parity(eng),
        _check_execution_rate(ev),
        _check_signal_mix(ev),
        _check_double_execution(ev),
        _check_one_per_pair(trades),
        _check_position_limit(trades, eq),
        _check_risk_pct(trades),
        _perf_vs_backtest(trades, eq),
    ]
    tally = {s: sum(1 for c in checks if c["status"] == s) for s in (_OK, _WARN, _FAIL, _INFO)}
    if tally[_FAIL]:
        verdict = "fail"
        headline = "バックテスト前提から外れた挙動を検出"
    elif tally[_WARN]:
        verdict = "warn"
        headline = "一部にバックテストとの乖離あり"
    else:
        verdict = "ok"
        headline = "バックテスト前提どおりに稼働"

    return {
        "generated_at": _now().isoformat(),
        "purpose": (
            "資産推移ではなく、実運用がバックテスト(4戦略統合)の前提どおりに"
            "取引できているかを検証する。"
        ),
        "verdict": verdict,
        "headline": headline,
        "tally": tally,
        "baseline": baseline.as_dict(),
        "checks": checks,
    }
