"""FastAPI アプリ本体。

- `/`            : ダッシュボード HTML
- `/api/*`       : 集計 JSON(すべて cache.get_or_set 経由)
- `/healthz`     : liveness(プロセスが生きているか)  ← ALB / ECS のヘルスチェック用
- `/readyz`      : readiness(DB に到達できるか)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.requests import Request

from . import __version__, aggregations, cache, reconciliation
from .cloudwatch import get_alarm_states
from .config import get_settings
from .db import get_session, init_db

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="通貨自動売買ボット 監視ダッシュボード", version=__version__)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.on_event("startup")
def _startup() -> None:
    # デモ / SQLite 用。本番は Alembic マイグレーションを想定。
    init_db()


# --------------------------------------------------------------------------- #
# ヘルスチェック
# --------------------------------------------------------------------------- #
@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/readyz", include_in_schema=False)
def readyz(session: Session = Depends(get_session)) -> JSONResponse:
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:
        return JSONResponse(
            {"status": "unready", "error": type(exc).__name__}, status_code=503
        )
    return JSONResponse({"status": "ready"})


# --------------------------------------------------------------------------- #
# ダッシュボード
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request) -> HTMLResponse:
    settings = get_settings()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "version": __version__,
            "instance_id": settings.ec2_instance_id,
            "region": settings.aws_region,
        },
    )


# --------------------------------------------------------------------------- #
# API(集計はすべて TTL キャッシュ経由)
# --------------------------------------------------------------------------- #
@app.get("/api/reconciliation")
def api_reconciliation() -> dict:
    """バックテスト整合性チェック(このダッシュボードの主目的)。"""
    return cache.get_or_set("agg:reconciliation", reconciliation.reconcile)


@app.get("/api/backtest-parity")
def api_backtest_parity(session: Session = Depends(get_session)) -> dict:
    """`backtest/replay.py` の最新照合レポート。signal(entry)/ trade(exit・損益)の両方。"""
    import json

    from sqlalchemy import select

    from .models import ParityRun

    def _serialize(pr: "ParityRun | None") -> dict:
        if pr is None:
            return {"available": False}
        return {
            "available": True,
            "ran_at": pr.ran_at.isoformat(),
            "mode": pr.mode,
            "ohlc_source": pr.ohlc_source,
            "bars": pr.bars,
            "period_start": pr.period_start.isoformat() if pr.period_start else None,
            "period_end": pr.period_end.isoformat() if pr.period_end else None,
            "expected_n": pr.expected_n,
            "actual_n": pr.actual_n,
            "matched_n": pr.matched_n,
            "missing_n": pr.missing_n,
            "extra_n": pr.extra_n,
            "mismatch_n": pr.mismatch_n,
            "match_rate": pr.match_rate,
            "detail": json.loads(pr.detail_json or "{}"),
        }

    def _latest(mode: str) -> "ParityRun | None":
        return session.scalars(
            select(ParityRun).where(ParityRun.mode == mode)
            .order_by(ParityRun.ran_at.desc()).limit(1)
        ).first()

    def _load() -> dict:
        return {
            "signal": _serialize(_latest("signal")),
            "trade": _serialize(_latest("trade")),
        }

    return cache.get_or_set("agg:backtest_parity", _load, ttl=30)


@app.get("/api/summary")
def api_summary() -> dict:
    return cache.get_or_set("agg:summary", aggregations.summary)


@app.get("/api/equity-curve")
def api_equity_curve() -> list:
    return cache.get_or_set("agg:equity_curve", aggregations.equity_curve)


@app.get("/api/daily-pnl")
def api_daily_pnl() -> list:
    return cache.get_or_set("agg:daily_pnl", aggregations.daily_pnl)


@app.get("/api/by-strategy")
def api_by_strategy() -> list:
    return cache.get_or_set("agg:by_strategy", aggregations.by_strategy)


@app.get("/api/by-pair")
def api_by_pair() -> list:
    return cache.get_or_set("agg:by_pair", aggregations.by_pair)


@app.get("/api/signal-stats")
def api_signal_stats() -> dict:
    return cache.get_or_set("agg:signal_stats", aggregations.signal_stats)


@app.get("/api/recent-events")
def api_recent_events(limit: int = Query(50, ge=1, le=500)) -> list:
    return cache.get_or_set(
        f"agg:recent_events:{limit}", lambda: aggregations.recent_events(limit)
    )


@app.get("/api/open-positions")
def api_open_positions() -> list:
    return cache.get_or_set("agg:open_positions", aggregations.open_positions)


@app.get("/api/cloudwatch")
def api_cloudwatch() -> dict:
    # CloudWatch は外部 API のため短め TTL
    return cache.get_or_set("agg:cloudwatch", get_alarm_states, ttl=30)
