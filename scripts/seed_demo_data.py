"""デモデータ生成。

実運用ログが無くてもダッシュボードが動く状態にするため、約 120 日ぶんのデータを
決定論的に生成する。デモ全体を内部整合させるため、次の順で作る:

  デモ OHLC(synthetic candles)
    → バックテスト戦略(backtest.strategies)で「期待シグナル」を再現
    → その一部を落とす/方向を反転/余分を足す(実運用の乖離を模倣)して signal_events 化
    → 発注結果(証拠金不足 / max_positions スキップ / 約定→トレード)
    → 最後に backtest.replay で期待シグナルと実 signal_events を突合し parity_runs へ保存

これにより、ダッシュボードの「バックテスト整合性チェック」と「バックテスト再現(トレード単位)」
の両パネルが意味のある内容で表示される(verdict=fail / 一致率 ~85%)。

使い方:  python -m scripts.seed_demo_data
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.db import SessionLocal, engine, init_db
from app.models import AppMeta, Base, BotHeartbeat, EquitySnapshot, SignalEvent, Trade
from backtest import exits as exits_mod
from backtest import ohlc as ohlc_mod
from backtest import replay as replay_mod
from backtest.strategies import generate_all_signals

SEED = 20260907
DAYS = 120
START_BALANCE = 300_000.0
SIZING_BALANCE = 300_000.0   # リスク額は複利で膨張させない(デモ数値の発散防止)

PAIRS = [
    "USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY", "EUR_USD", "GBP_USD",
    "AUD_USD", "NZD_JPY", "CAD_JPY", "CHF_JPY", "EUR_GBP", "NZD_USD",
]
WATCH_PAIRS = {"AUD_JPY", "AUD_USD", "CAD_JPY"}
STRATEGIES = ["SB", "MACD", "DM_PSAR", "BB_SQ"]
RISK_PCT = {"SB": 0.0225, "MACD": 0.01125, "DM_PSAR": 0.01125, "BB_SQ": 0.01125}
BASE_PRICE = {p: (145.0 if p.endswith("JPY") else 1.25) for p in PAIRS}

MARGIN_INSUFFICIENT_RATE = 0.55
WIN_RATE = 0.41
MAX_POSITIONS = 6        # 本番コードのハードコード値を再現(方針は「上限なし」)
DOUBLE_EXEC_RATE = 0.02  # 二重稼働による重複約定
MISS_RATE = 0.09        # 期待シグナルをボットが検出しそこねる(→ parity MISSING)
MISMATCH_RATE = 0.03    # 方向を取り違える(→ parity MISMATCH)
EXTRA_RATE = 0.015      # バックテストにないシグナルをボットが出す(→ parity EXTRA)


def _reset() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _flip(side: str) -> str:
    return "SHORT" if side == "LONG" else "LONG"


def _pnl(rng: random.Random, pair: str, risk_jpy: float) -> tuple[float, str]:
    win_rate = WIN_RATE - (0.10 if pair in WATCH_PAIRS else 0.0)
    if rng.random() < win_rate:
        return round(risk_jpy * rng.uniform(1.05, 1.9), 0), rng.choice(["TP", "TRAIL"])
    return round(-risk_jpy * rng.uniform(0.9, 1.12), 0), rng.choice(["SL", "LOSSCUT"])


def seed() -> dict[str, int]:
    rng = random.Random(SEED)
    init_db()
    _reset()

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=DAYS)

    # --- デモ OHLC と期待シグナル(replay と同じ入力)---
    ohlc = ohlc_mod.make_demo_ohlc(PAIRS, start, now, seed=SEED)
    expected = generate_all_signals(ohlc)
    exp_by_hour: dict[datetime, list] = defaultdict(list)
    for e in expected:
        key = e.ts.to_pydatetime().replace(minute=0, second=0, microsecond=0)
        exp_by_hour[key].append(e)

    # 実トレードの損益は、同じ OHLC でのエグジット再現(=バックテスト想定の結果)を土台にする。
    # そのうえで一部を「フィード差・手動介入」で乱し、trade parity が ~85% 一致になるようにする。
    _sims = exits_mod.simulate_trades(ohlc, expected)
    sim_map = {
        (s.pair, s.strategy, s.side, pd.Timestamp(s.signal_ts).to_pydatetime()
         .replace(minute=0, second=0, microsecond=0)): s
        for s in _sims
    }

    session = SessionLocal()
    balance = START_BALANCE
    next_pos_id = 9_000_000
    open_trades: list[dict] = []
    counts = {
        "heartbeats": 0, "equity": 0, "signals": 0, "orders": 0, "exits": 0,
        "trades": 0, "expected_signals": len(expected),
    }

    cycle = start
    while cycle <= now:
        cid = cycle.strftime("%Y%m%d-%H%M")

        # --- 満期ポジションの決済 ---
        still_open = []
        for item in open_trades:
            if item["exit_at"] <= cycle:
                tr: Trade = item["trade"]
                risk_jpy = (tr.risk_pct or 0.01125) * SIZING_BALANCE
                if "pnl" in item:
                    pnl, reason = item["pnl"], item["reason"]
                else:
                    pnl, reason = _pnl(rng, tr.pair, risk_jpy)
                tr.exit_time = item["exit_at"]
                tr.exit_price = round(tr.entry_price * (1 + rng.uniform(-0.01, 0.01)), 5)
                tr.pnl_jpy = pnl
                tr.exit_reason = reason
                tr.status = "closed"
                balance += pnl
                session.add(SignalEvent(
                    ts=item["exit_at"], pair=tr.pair, strategy=tr.strategy, side=tr.side,
                    event_type="exit", result="CLOSED", position_id=tr.position_id,
                    price=tr.exit_price, pnl_jpy=pnl, reason=reason,
                    line_hash=f"seed-exit-{tr.position_id}",
                ))
                counts["exits"] += 1
            else:
                still_open.append(item)
        open_trades = still_open

        session.add(BotHeartbeat(ts=cycle, cycle_id=cid, pairs_checked=len(PAIRS)))
        counts["heartbeats"] += 1

        # --- このサイクルのシグナル(= 期待シグナル ± 乖離)---
        due = list(exp_by_hour.get(cycle, []))

        # EXTRA: バックテストにないシグナルをボットが出すことがある
        if rng.random() < EXTRA_RATE:
            due.append(_FakeSig(
                pair=rng.choice(PAIRS), strategy=rng.choice(STRATEGIES),
                side=rng.choice(["LONG", "SHORT"]), extra=True,
            ))

        for e in due:
            is_extra = getattr(e, "extra", False)
            if not is_extra and rng.random() < MISS_RATE:
                continue  # ボットが検出しそこねた → parity MISSING

            pair, strat = e.pair, e.strategy
            side = e.side
            if not is_extra and rng.random() < MISMATCH_RATE:
                side = _flip(side)  # 方向取り違え → parity MISMATCH

            risk_pct = RISK_PCT[strat]
            price = round(
                BASE_PRICE[pair] * (1 + rng.uniform(-0.03, 0.03)),
                3 if pair.endswith("JPY") else 5,
            )
            lot = round(max(1000, SIZING_BALANCE * risk_pct / max(price, 0.01)) // 1000 * 1000)

            session.add(SignalEvent(
                ts=cycle, pair=pair, strategy=strat, side=side,
                event_type="signal", risk_pct=risk_pct,
                detail="extra_signal" if is_extra else "entry_signal",
                line_hash=f"seed-signal-{cid}-{pair}-{strat}-{rng.random()}",
            ))
            counts["signals"] += 1

            # 1 ペア 1 ポジション制約
            if any(t["trade"].pair == pair for t in open_trades):
                continue

            if rng.random() < MARGIN_INSUFFICIENT_RATE:
                session.add(SignalEvent(
                    ts=cycle, pair=pair, strategy=strat, side=side,
                    event_type="order", result="MARGIN_INSUFFICIENT",
                    detail="Trading margin is insufficient",
                    line_hash=f"seed-order-fail-{cid}-{pair}-{strat}-{rng.random()}",
                ))
                counts["orders"] += 1
                continue

            if len(open_trades) >= MAX_POSITIONS:
                session.add(SignalEvent(
                    ts=cycle, pair=pair, strategy=strat, side=side,
                    event_type="order", result="SKIPPED",
                    detail=f"max_positions({MAX_POSITIONS}) reached",
                    line_hash=f"seed-order-skip-{cid}-{pair}-{strat}-{rng.random()}",
                ))
                counts["orders"] += 1
                continue

            # このシグナルに対応する「バックテスト想定のトレード結果」を引く
            sim = sim_map.get((pair, strat, side, cycle))

            def _open(pos_id: str) -> None:
                session.add(SignalEvent(
                    ts=cycle, pair=pair, strategy=strat, side=side,
                    event_type="order", result="EXECUTED", position_id=pos_id,
                    price=price, lot=lot, risk_pct=risk_pct,
                    line_hash=f"seed-order-ok-{pos_id}",
                ))
                counts["orders"] += 1
                tr = Trade(
                    position_id=pos_id, pair=pair, strategy=strat, side=side,
                    entry_time=cycle, entry_price=price, lot=lot, risk_pct=risk_pct,
                    status="open",
                )
                session.add(tr)
                counts["trades"] += 1

                risk_jpy = risk_pct * SIZING_BALANCE
                if sim is not None:
                    # バックテスト想定を土台に、実運用の大きな目減り(スプレッド/約定ズレ/
                    # 手動介入/フィード差/証拠金による部分約定)を反映。
                    # 勝ち想定の多くが負け・微益に転び、実運用ではほぼトントン〜微益になる。
                    r = sim.pnl_r * 0.4
                    reason = sim.exit_reason
                    if sim.pnl_r > 0 and rng.random() < 0.40:
                        r = rng.uniform(-1.05, 0.15)
                        reason = "SL" if r < 0 else ("MANUAL" if rng.random() < 0.5 else reason)
                    elif sim.pnl_r <= 0 and rng.random() < 0.08:
                        r = rng.uniform(0.0, 0.6)
                        reason = "MANUAL"
                    pnl = round(r * risk_jpy, 0)
                    exit_at = sim.exit_ts if sim.exit_ts > cycle else cycle + timedelta(hours=rng.randint(2, 72))
                else:
                    pnl, reason = _pnl(rng, pair, risk_jpy)
                    exit_at = cycle + timedelta(hours=rng.randint(2, 72))
                open_trades.append(
                    {"trade": tr, "exit_at": exit_at, "pnl": pnl, "reason": reason}
                )

            next_pos_id += 1
            _open(str(next_pos_id))

            # 二重稼働: まれに同一サイクル・同一(ペア×手法)で連番 positionId が重複約定
            if rng.random() < DOUBLE_EXEC_RATE:
                next_pos_id += 1
                _open(str(next_pos_id))

        # --- 口座スナップショット ---
        margin_used = sum(
            (t["trade"].risk_pct or 0.01125) * SIZING_BALANCE * 4 for t in open_trades
        )
        equity = balance + rng.uniform(-1500, 1500)
        margin_ratio = round(equity / margin_used * 100, 1) if margin_used > 0 else None
        session.add(EquitySnapshot(
            ts=cycle, balance_jpy=round(balance, 0), equity_jpy=round(equity, 0),
            margin_used_jpy=round(margin_used, 0), margin_ratio=margin_ratio,
            open_positions=len(open_trades),
        ))
        counts["equity"] += 1

        cycle += timedelta(hours=1)

    session.merge(AppMeta(
        key="data_mode",
        value=f"demo (合成データ・{now:%Y-%m-%d} 生成)",
    ))
    session.commit()

    # --- バックテスト再現を実行して parity_runs へ(signal / trade 両方)---
    src = "demo (synthetic OHLC, 120d/1h)"
    pr_s = replay_mod.run(ohlc, session, src)
    counts["signal_parity_match_pct"] = round(pr_s.match_rate * 100)
    counts["signal_parity_missing"] = pr_s.missing_n
    counts["signal_parity_extra"] = pr_s.extra_n
    pr_t = replay_mod.run_trades(ohlc, session, src)
    counts["trade_parity_outcome_agree_pct"] = round(pr_t.match_rate * 100)
    counts["trade_parity_sim"] = pr_t.expected_n
    counts["trade_parity_missing"] = pr_t.missing_n
    counts["trade_parity_extra"] = pr_t.extra_n
    session.close()
    return counts


class _FakeSig:
    """EXTRA シグナル用の軽量オブジェクト(ExpectedSignal と同じ属性)。"""

    def __init__(self, pair: str, strategy: str, side: str, extra: bool = False):
        self.pair = pair
        self.strategy = strategy
        self.side = side
        self.extra = extra


if __name__ == "__main__":
    result = seed()
    print("デモデータを生成しました:")
    for k, v in result.items():
        print(f"  {k:22} {v}")
