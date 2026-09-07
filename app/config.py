"""環境変数ベースの設定。

12-factor に寄せて、接続先(DB / Redis / AWS)はすべて環境変数から解決する。
ローカルは何も設定しなくても SQLite + プロセス内キャッシュで起動できる。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- データベース(RDS/PostgreSQL 相当。既定はゼロ設定の SQLite)---
    database_url: str = "sqlite:///./fxbot_dashboard.db"

    # --- キャッシュ(ElastiCache/Redis 相当。未設定ならプロセス内メモリ)---
    redis_url: str | None = None
    cache_ttl_seconds: int = 60

    # --- AWS / CloudWatch(既定はプレースホルダ。実環境の値は .env で渡す)---
    aws_region: str = "ap-northeast-1"
    ec2_instance_id: str = "i-0123456789abcdef0"
    cloudwatch_alarm_names: str = (
        "my-bot-SystemStatusFailed-AutoRecover,my-bot-StopAlert"
    )

    # --- ログ取り込み ---
    fxbot_log_path: str = "./ingest/sample_fxbot.log"
    ingest_state_path: str = "./.ingest_state.json"

    # --- ボット死活判定(hourly 実行前提)---
    bot_cycle_interval_minutes: int = 60
    bot_stale_after_minutes: int = 150

    @property
    def alarm_name_list(self) -> list[str]:
        return [x.strip() for x in self.cloudwatch_alarm_names.split(",") if x.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
