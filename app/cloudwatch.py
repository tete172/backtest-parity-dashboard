"""CloudWatch アラーム状態の取得。

監視ボットの EC2(物理ホスト障害 → ec2:recover 自動復旧)の CloudWatch アラームを
ダッシュボードから見えるようにする。認証情報が無い環境でも落とさず、
{"available": False} を返してパネルだけ "利用不可" 表示にする。
"""

from __future__ import annotations

from typing import Any

from .config import get_settings


def get_alarm_states() -> dict[str, Any]:
    settings = get_settings()
    try:
        import boto3

        cw = boto3.client("cloudwatch", region_name=settings.aws_region)
        resp = cw.describe_alarms(AlarmNames=settings.alarm_name_list)
        alarms = [
            {
                "name": a["AlarmName"],
                "state": a["StateValue"],  # OK / ALARM / INSUFFICIENT_DATA
                "reason": a.get("StateReason", ""),
                "updated": a.get("StateUpdatedTimestamp"),
            }
            for a in resp.get("MetricAlarms", [])
        ]
        return {"available": True, "region": settings.aws_region, "alarms": alarms}
    except Exception as exc:  # 認証情報なし / 権限なし / ネットワーク不通
        return {
            "available": False,
            "error": type(exc).__name__,
            "region": settings.aws_region,
            "alarms": [],
        }
