"""GMO コイン FX の約定履歴を取り込んで trades の決済価格・損益を埋める。

fxbot.log には決済(GMO 側 OCO / SL)の価格・損益が残らないため、これで補完する:

  python -m ingest.gmo_history --api           # GMO Private API(直近1ヶ月ぶん)
  python -m ingest.gmo_history --csv trades.csv # 正規化済み CSV(古い期間はこちら)

positionId で `trades` と突合し、CLOSE 約定の lossGain を pnl_jpy、price を exit_price に入れる。
OPEN 約定は entry_price が未設定の trade を補完する。

API 認証は監視対象ボットと同じ HMAC-SHA256(署名対象パスは `/private` を除いた `/v1/...`)。
キーは環境変数 or `.env` の GMO_API_KEY / GMO_API_SECRET(公開リポジトリにはコミットしない)。
依存は標準ライブラリのみ(requests 等は不要。TLS の CA 検証は certifi があれば使用)。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import ssl
import time
import urllib.request
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cache import clear as clear_cache
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import AppMeta, Trade

_BASE_PRIVATE = "https://forex-api.coin.z.com/private"
_SYMBOLS = [
    "USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY", "NZD_JPY", "EUR_USD", "GBP_USD",
    "AUD_USD", "NZD_USD", "CAD_JPY", "EUR_GBP", "CHF_JPY",
]


# --------------------------------------------------------------------------- #
# 変換ヘルパ
# --------------------------------------------------------------------------- #
def _f(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _ts(v) -> datetime | None:
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _entry_side_from_close(close_side: str | None) -> str:
    # 決済約定の side はエントリーと逆(BUYで入った建玉は SELL で決済)
    return "SHORT" if _norm_side(close_side) == "BUY" else "LONG"


# --------------------------------------------------------------------------- #
# API 取得
# --------------------------------------------------------------------------- #
def _ssl_context() -> ssl.SSLContext:
    """TLS 証明書検証は有効のまま。Windows で OS の CA を拾えない場合は certifi を使う。"""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _headers(secret: str, key: str, method: str, path: str, body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    sign = hmac.new(
        secret.encode(), (ts + method + path + body).encode(), hashlib.sha256
    ).hexdigest()
    return {"API-KEY": key, "API-TIMESTAMP": ts, "API-SIGN": sign}


def fetch_executions(key: str, secret: str, symbols=None, max_pages: int = 30) -> list[dict]:
    """latestExecutions を全ペア・全ページ取得(直近約1ヶ月ぶん)。"""
    out: list[dict] = []
    ctx = _ssl_context()
    first = True
    for sym in symbols or _SYMBOLS:
        for page in range(1, max_pages + 1):
            if not first:
                time.sleep(1.1)  # Private API は概ね 1 req/s。全リクエスト間で待つ
            first = False
            path = "/v1/latestExecutions"
            url = f"{_BASE_PRIVATE}{path}?symbol={sym}&page={page}&count=100"
            req = urllib.request.Request(url, headers=_headers(secret, key, "GET", path))
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:  # noqa: S310
                data = json.loads(r.read().decode())
            if data.get("status") != 0:
                raise RuntimeError(f"{sym} p{page}: {data.get('messages') or data}")
            lst = data.get("data", {}).get("list", []) or []
            out.extend(lst)
            if len(lst) < 100:
                break
    return out


# --------------------------------------------------------------------------- #
# CSV 取得(正規化済み: positionId,symbol,side,settleType,price,size,lossGain,timestamp)
# --------------------------------------------------------------------------- #
_CSV_ALIASES = {
    "positionId": ("positionid", "ポジションid", "建玉番号", "建玉id", "ポジション番号"),
    "symbol": ("symbol", "通貨ペア", "銘柄", "商品"),
    "side": ("side", "売買", "売買区分"),
    "settleType": ("settletype", "決済区分", "取引区分", "新規決済区分", "区分"),
    "price": ("price", "約定rate", "約定レート", "約定価格", "レート"),
    "size": ("size", "約定数量", "数量", "取引数量"),
    "lossGain": ("lossgain", "決済損益", "実現損益", "損益", "決済損益(円)"),
    "timestamp": ("timestamp", "日時", "約定日時", "決済日時", "日付", "約定日", "取引日時", "決済日"),
}


def _norm_settle(v) -> str:
    """CSV/API の取引区分を OPEN / CLOSE に正規化する。"""
    s = str(v or "").strip().lower()
    if s in ("close", "決済", "清算", "精算", "settle", "決済注文"):
        return "CLOSE"
    if s in ("open", "新規", "新規注文"):
        return "OPEN"
    return s.upper()


def _norm_side(v) -> str:
    s = str(v or "").strip().lower()
    if s in ("buy", "買", "買い", "b", "long"):
        return "BUY"
    if s in ("sell", "売", "売り", "s", "short"):
        return "SELL"
    return s.upper()


def parse_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return []
    lower = {c.lower().strip(): c for c in rows[0].keys()}

    def col(field: str) -> str | None:
        for alias in _CSV_ALIASES[field]:
            if alias in lower:
                return lower[alias]
        return None

    m = {f: col(f) for f in _CSV_ALIASES}
    out = []
    for r in rows:
        out.append({k: (r.get(v) if v else None) for k, v in m.items()})
    return out


# --------------------------------------------------------------------------- #
# 適用
# --------------------------------------------------------------------------- #
def apply_executions(session: Session, execs: list[dict]) -> dict[str, int]:
    counts = {"close_matched": 0, "close_new": 0, "open_filled": 0, "skipped": 0}
    for e in execs:
        pid = str(e.get("positionId") or "").strip()
        if not pid:
            counts["skipped"] += 1
            continue
        stype = _norm_settle(e.get("settleType"))
        px = _f(e.get("price"))
        ts = _ts(e.get("timestamp"))
        tr = session.scalar(select(Trade).where(Trade.position_id == pid))

        if stype == "CLOSE":
            if tr is None:
                tr = Trade(
                    position_id=pid, pair=e.get("symbol") or "-", strategy="?",
                    side=_entry_side_from_close(e.get("side")),
                    entry_time=ts or datetime.now(timezone.utc),
                    entry_price=0.0, lot=_f(e.get("size")) or 0.0, status="open",
                )
                session.add(tr)
                counts["close_new"] += 1
            else:
                counts["close_matched"] += 1
            tr.exit_time = ts
            tr.exit_price = px
            tr.pnl_jpy = _f(e.get("lossGain"))
            tr.exit_reason = "GMO約定履歴"
            tr.status = "closed"
            session.flush()

        elif stype == "OPEN":
            if tr is not None and not tr.entry_price and px:
                tr.entry_price = px
                if not tr.lot:
                    tr.lot = _f(e.get("size")) or 0.0
                session.flush()
                counts["open_filled"] += 1
        else:
            counts["skipped"] += 1
    return counts


def run(execs: list[dict]) -> dict[str, int]:
    session = SessionLocal()
    try:
        c = apply_executions(session, execs)
        session.merge(AppMeta(key="data_mode", value="live (実ログ + GMO約定履歴)"))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    clear_cache()
    return c


def main() -> None:
    ap = argparse.ArgumentParser(description="GMO 約定履歴で trades の損益を補完")
    ap.add_argument("--api", action="store_true", help="GMO Private API から取得(直近1ヶ月)")
    ap.add_argument("--csv", help="正規化済み約定履歴 CSV のパス(古い期間用)")
    args = ap.parse_args()

    init_db()
    if args.api:
        s = get_settings()
        key, secret = s.gmo_api_key, s.gmo_api_secret
        if not (key and secret):
            raise SystemExit(
                "GMO_API_KEY / GMO_API_SECRET を .env か環境変数で設定してください。"
            )
        execs = fetch_executions(key, secret)
    elif args.csv:
        execs = parse_csv(args.csv)
    else:
        ap.error("--api か --csv を指定してください")
        return

    print(f"約定 {len(execs)} 件取得 → 適用: {run(execs)}")


if __name__ == "__main__":
    main()
