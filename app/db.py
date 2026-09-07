"""SQLAlchemy エンジン / セッション。

SQLite と PostgreSQL(RDS)のどちらでも動くように、方言依存の集計は SQL ではなく
pandas 側(aggregations.py)で行う方針。
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

settings = get_settings()

_connect_args: dict = {}
if settings.database_url.startswith("sqlite"):
    # FastAPI のスレッドプールから触るため
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, future=True
)


def get_session() -> Iterator[Session]:
    """FastAPI の Depends 用セッション。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """テーブルが無ければ作成(デモ / SQLite 用。本番は Alembic を想定)。"""
    from .models import Base

    Base.metadata.create_all(bind=engine)
