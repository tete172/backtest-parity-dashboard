"""キャッシュ層(ElastiCache/Redis 相当)。

設計方針:
- Redis は「あれば使う」補助であって必須依存にしない。
  REDIS_URL 未設定・接続失敗・実行時エラーのいずれでも、プロセス内 TTL キャッシュに
  透過的にフォールバックし、ダッシュボードは止めない。
- ダッシュボードの集計(pandas)は数十〜数百 ms かかるため、TTL 60 秒で十分効く。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

from .config import get_settings

settings = get_settings()

_local: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()

_redis = None
if settings.redis_url:
    try:  # pragma: no cover - 接続可否は環境依存
        import redis

        _redis = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=1, socket_timeout=1
        )
        _redis.ping()
    except Exception:
        _redis = None


def backend() -> str:
    return "redis" if _redis is not None else "in-process"


def _get(key: str) -> str | None:
    if _redis is not None:
        try:  # pragma: no cover
            val = _redis.get(key)
            return val.decode() if val is not None else None
        except Exception:
            pass  # Redis が落ちたらローカルに切り替える
    with _lock:
        entry = _local.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.time():
            _local.pop(key, None)
            return None
        return value


def _set(key: str, value: str, ttl: int) -> None:
    if _redis is not None:
        try:  # pragma: no cover
            _redis.setex(key, ttl, value)
            return
        except Exception:
            pass
    with _lock:
        _local[key] = (time.time() + ttl, value)


def get_or_set(key: str, producer: Callable[[], Any], ttl: int | None = None) -> Any:
    """key があれば返し、無ければ producer() を実行して保存してから返す。"""
    ttl = settings.cache_ttl_seconds if ttl is None else ttl
    cached = _get(key)
    if cached is not None:
        return json.loads(cached)
    value = producer()
    _set(key, json.dumps(value, default=str, ensure_ascii=False), ttl)
    return value


def clear() -> None:
    """取り込み後などに明示的に無効化する用。"""
    with _lock:
        _local.clear()
    if _redis is not None:
        try:  # pragma: no cover
            _redis.flushdb()
        except Exception:
            pass
