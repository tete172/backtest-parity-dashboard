"""バックテストの前提。

⚠️ 本リポジトリは公開ポートフォリオです。ここのパラメータ・実績値は
**サンプル(一般的な値・仮の数値)**で、監視対象ボットの実値ではありません(実値は非公開)。
狙いは「実運用がバックテストの前提どおりに動いているか」を機械的に突き合わせる
仕組みを示すこと。`app/reconciliation.py` がここの値と実データを突き合わせる。
実運用ではこのモジュールを自分のバックテスト設定で置き換える。
"""

from __future__ import annotations

# --- 手法別リスク%(サンプル)---
STRATEGY_RISK_PCT: dict[str, float] = {
    "SB": 0.02,
    "MACD": 0.01,
    "DM_PSAR": 0.01,
    "BB_SQ": 0.01,
}

# --- 発注の優先順位(1 ペア 1 ポジション)---
STRATEGY_PRIORITY: list[str] = ["SB", "MACD", "DM_PSAR", "BB_SQ"]
K_FACTOR = 1.0  # ポジションサイズ係数(サンプル)

# --- バックテストのシグナル発生比率(サンプル。バックテストのトレード件数比から算出する想定)---
BACKTEST_SIGNAL_COUNTS: dict[str, int] = {
    "SB": 4000,
    "MACD": 2500,
    "DM_PSAR": 1200,
    "BB_SQ": 600,
}
_total = sum(BACKTEST_SIGNAL_COUNTS.values())
EXPECTED_SIGNAL_MIX: dict[str, float] = {
    k: round(v / _total, 4) for k, v in BACKTEST_SIGNAL_COUNTS.items()
}

# --- 同時ポジション制約 ---
# 方針は「上限なし」だが、監視対象ボットのコードで max_positions がハードコードされていた
# (方針との食い違いを検出するためのサンプル値)。
POSITION_LIMIT_DOCUMENTED: int | None = None
POSITION_LIMIT_IN_CODE = 6
ONE_POSITION_PER_PAIR = True

# --- パフォーマンス(サンプル。実バックテスト値ではない)---
BACKTEST_CAGR_PCT = 40.0
BACKTEST_MAX_DD_PCT = 50.0
BACKTEST_WORST_YEAR_PCT = -20.0
SLIPPAGE_PIP = 0.5

# --- 乖離判定のしきい値 ---
SIGNAL_MIX_TOLERANCE = 0.15   # 実測比率が期待比率から ±15pt を超えたら要確認
EXECUTION_RATE_WARN = 0.90    # シグナル→約定の実行率(バックテストは全約定前提)
EXECUTION_RATE_FAIL = 0.50
MIN_TRADES_FOR_PERF_JUDGEMENT = 300  # これ未満は成績の善し悪しを判定しない(サンプル不足)


def as_dict() -> dict:
    """ダッシュボードに前提を表示する用。"""
    return {
        "strategy_risk_pct": STRATEGY_RISK_PCT,
        "strategy_priority": STRATEGY_PRIORITY,
        "k_factor": K_FACTOR,
        "expected_signal_mix": EXPECTED_SIGNAL_MIX,
        "position_limit_documented": POSITION_LIMIT_DOCUMENTED,
        "position_limit_in_code": POSITION_LIMIT_IN_CODE,
        "one_position_per_pair": ONE_POSITION_PER_PAIR,
        "backtest_cagr_pct": BACKTEST_CAGR_PCT,
        "backtest_max_dd_pct": BACKTEST_MAX_DD_PCT,
        "slippage_pip": SLIPPAGE_PIP,
        "note": "値はサンプル。実運用では自分のバックテスト設定に置き換える。",
    }
