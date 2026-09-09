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
_OPP_GAP_HOURS = 3  # これ以内の連続した同一(ペア×手法)の発注試行は 1 取引機会にまとめる


def _opportunities(ev: pd.DataFrame) -> pd.DataFrame:
    """連続する同一(ペア×手法)の発注試行を 1 つの「取引機会」に畳む。

    ライブのボットは相場条件が続く限り毎時「シグナル！」を出すため、証拠金不足で
    入れないと同じセットアップが何度もログされる(バックテストなら 1 トレード)。
    実行率・比率はこの畳み込み後の機会数で見ないと過大評価になる。
    """
    orders = ev[ev["event_type"] == "order"].copy() if not ev.empty else pd.DataFrame()
    if orders.empty:
        return pd.DataFrame()
    orders["ts"] = pd.to_datetime(orders["ts"], utc=True)
    orders = orders.sort_values(["pair", "strategy", "ts"])
    gap = pd.Timedelta(hours=_OPP_GAP_HOURS)
    rows: list[dict] = []
    for (pair, strat), g in orders.groupby(["pair", "strategy"], dropna=False):
        cur = None
        prev_ts = None
        for _, r in g.iterrows():
            if cur is None or (r["ts"] - prev_ts) > gap:
                if cur is not None:
                    rows.append(cur)
                cur = {"pair": pair, "strategy": strat, "start": r["ts"],
                       "attempts": 0, "executed": 0, "insufficient": 0, "other": 0}
            cur["attempts"] += 1
            res = r["result"]
            if res == "EXECUTED":
                cur["executed"] += 1
            elif res == "MARGIN_INSUFFICIENT":
                cur["insufficient"] += 1
            else:
                cur["other"] += 1
            prev_ts = r["ts"]
        if cur is not None:
            rows.append(cur)
    df = pd.DataFrame(rows)
    df["outcome"] = df.apply(
        lambda x: "taken" if x["executed"] > 0
        else "missed_margin" if x["insufficient"] > 0
        else "missed_other", axis=1,
    )
    return df


def _check_execution_rate(ev: pd.DataFrame) -> dict:
    opp = _opportunities(ev)
    if opp.empty:
        return _check("execution_rate", "取引機会 → 約定の実行率", _INFO,
                      "≈100%(バックテストは全機会で約定前提)", "データなし")
    n = len(opp)
    taken = int((opp["outcome"] == "taken").sum())
    missed_margin = int((opp["outcome"] == "missed_margin").sum())
    missed_other = int((opp["outcome"] == "missed_other").sum())
    raw_attempts = int(opp["attempts"].sum())
    rate = taken / n if n else 0.0
    if rate < baseline.EXECUTION_RATE_FAIL:
        status = _FAIL
    elif rate < baseline.EXECUTION_RATE_WARN:
        status = _WARN
    else:
        status = _OK
    return _check(
        "execution_rate", "取引機会 → 約定の実行率", status,
        f"≥ {baseline.EXECUTION_RATE_WARN:.0%}",
        f"{rate:.1%}",
        f"取引機会 {n} 件中 約定 {taken} / 証拠金不足で見送り {missed_margin} / "
        f"その他失敗 {missed_other}。"
        f"(生ログの発注試行は {raw_attempts} 回だが、証拠金不足による毎時の再試行を "
        f"±{_OPP_GAP_HOURS}h で 1 機会にまとめた。バックテストは全機会で約定する前提。)",
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
    """手法別の実効リスク%(= risk円 / 有効証拠金)を中央値で並記。

    実運用では有効証拠金が大きく変動する(0円到達もあり)ため厳密一致は求めず、
    中央値がおおむね設定どおりかを目安で見る(このリポジトリの baseline はサンプル値)。
    """
    df = trades if not trades.empty else pd.DataFrame()
    if df.empty or df["risk_pct"].dropna().empty:
        return _check("risk_pct", "手法別リスク%(中央値)", _INFO,
                      baseline.STRATEGY_RISK_PCT, "risk_pct 未記録")
    med = {}
    for strat, grp in df.groupby("strategy"):
        v = grp["risk_pct"].dropna()
        if len(v):
            med[strat] = round(float(v.median()), 4)
    # 桁が明らかにおかしい(0.5% 未満 or 15% 超)ものだけ警告
    bad = [f"{k}={v:.2%}" for k, v in med.items() if not (0.005 <= v <= 0.15)]
    status = _WARN if bad else _INFO
    return _check(
        "risk_pct", "手法別リスク%(中央値)", status,
        {k: f"{v:.2%}" for k, v in baseline.STRATEGY_RISK_PCT.items()},
        {k: f"{v:.2%}" for k, v in med.items()},
        (f"想定外の桁: {', '.join(bad)}" if bad
         else "中央値はおおむね設定どおり(有効証拠金の変動で試行ごとにブレる)。"),
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
    limit = baseline.POSITION_LIMIT_IN_CODE
    # ボットが毎サイクル出力する「現在ポジション: N / 6」を信頼(実データの真値)。
    # trades 由来のスイープはログに決済記録が無いと過大になるためフォールバックのみ。
    if not eq.empty and "open_positions" in eq.columns and eq["open_positions"].notna().any():
        observed_max = int(eq["open_positions"].max())
        hit = int((eq["open_positions"] == observed_max).sum())
        src = "現在ポジション ログ"
    else:
        observed_max, hit = _sweep_max_concurrent(trades)
        src = "trades からスイープ"
    if observed_max >= limit:
        status = _WARN
        note = (
            f"最大同時建玉数 {observed_max}({src})。コードの CONFIG['max_positions']={limit} に達しており "
            f"(到達 {hit} 回)、方針「上限なし」と不一致。"
        )
    else:
        status = _OK
        note = f"最大同時建玉数 {observed_max}({src})。コードの上限 {limit} 未満で問題なし。"
    return _check(
        "position_limit", "同時建玉数の上限", status,
        f"上限なし(方針) / コードは {limit}",
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
    with_pnl = closed[closed["pnl_jpy"].notna()] if not closed.empty else pd.DataFrame()
    n = len(closed)
    live: dict[str, Any] = {"trades": n, "trades_with_pnl": int(len(with_pnl))}
    notes = []
    if len(with_pnl) >= 5:
        pnl = with_pnl["pnl_jpy"].astype(float)
        gp, gl = float(pnl[pnl > 0].sum()), float(-pnl[pnl <= 0].sum())
        live["win_rate"] = f"{(pnl > 0).mean():.1%}"
        live["profit_factor"] = round(gp / gl, 2) if gl else None
        live["net_pnl_jpy"] = round(float(pnl.sum()), 0)
    else:
        notes.append("損益は実ログに記録なし(GMO 約定履歴が必要)")

    zero_hit = 0
    if not eq.empty:
        e = eq.copy()
        e["ts"] = pd.to_datetime(e["ts"], utc=True)
        e = e.sort_values("ts")
        days = max((e["ts"].iloc[-1] - e["ts"].iloc[0]).days, 1)
        eqv = e["equity_jpy"].astype(float)
        zero_hit = int((eqv <= 0).sum())
        daily = e.set_index("ts")["equity_jpy"].resample("1D").last().dropna()
        if not daily.empty:
            live["max_drawdown_pct"] = round(float((daily / daily.cummax() - 1.0).min()) * 100, 1)
        live["equity_min_jpy"] = round(float(eqv.min()), 0)
        live["equity_max_jpy"] = round(float(eqv.max()), 0)
        live["observed_days"] = days
        if zero_hit:
            notes.append(f"有効証拠金が {zero_hit} 回 0 円に到達(実データ・その後入金で復帰)")

    status = _WARN if zero_hit else _INFO
    note = (
        f"運用 {live.get('observed_days', 0)} 日 / 決済済 {n} 取引。"
        + ("。".join([""] + notes) if notes else "")
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
