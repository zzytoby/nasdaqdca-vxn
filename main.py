"""
Nasdaq-100 DCA Discipline - VXN Data Proxy Backend
====================================================
数据源：CBOE 官方 VXN 历史数据（VXN 指数的官方发布方）
  https://cdn.cboe.com/api/global/us_indices/daily_prices/VXN_History.csv

稳定、无反爬、完整历史（2009 年至今）。为 iPhone App 提供 VXN 收盘数据。

Endpoints:
  GET /health              — 健康检查
  GET /vxn/current         — 最新 VXN 收盘值
  GET /vxn/history?days=N  — 历史 VXN 收盘数据（默认 365 天）
  GET /vxn/range?from=YYYY-MM-DD&to=YYYY-MM-DD — 指定日期范围
"""

from datetime import datetime, timedelta
import csv
import io
import logging

import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="NasdaqDCA VXN Proxy",
    description="VXN (Nasdaq-100 Volatility Index) data proxy via CBOE official feed",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VXN_SYMBOL = "^VXN"
CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VXN_History.csv"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 缓存（VXN 收盘价一天更新一次，缓存 10 分钟足够）
_cache: dict = {"data": None, "timestamp": None, "ttl_seconds": 600}


def _should_refresh() -> bool:
    if _cache["data"] is None or _cache["timestamp"] is None:
        return True
    return (datetime.utcnow() - _cache["timestamp"]).total_seconds() > _cache["ttl_seconds"]


def _fetch_records() -> list[dict]:
    """从 CBOE 官方源拉取 VXN 全量历史，返回记录列表（升序）"""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/csv,*/*"}
    resp = httpx.get(CBOE_URL, headers=headers, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()

    reader = csv.DictReader(io.StringIO(resp.text))
    records = []
    for row in reader:
        try:
            close = float(row.get("CLOSE") or 0)
            if close <= 0:
                continue
            dt = datetime.strptime(row["DATE"], "%m/%d/%Y")
            records.append({
                "date": dt.strftime("%Y-%m-%d"),
                "close": round(close, 2),
                "open": round(float(row["OPEN"]), 2) if row.get("OPEN") else None,
                "high": round(float(row["HIGH"]), 2) if row.get("HIGH") else None,
                "low": round(float(row["LOW"]), 2) if row.get("LOW") else None,
            })
        except (ValueError, KeyError, TypeError):
            continue

    if not records:
        raise HTTPException(status_code=502, detail="CBOE 数据为空或解析失败")

    return records


def _get_cached_records() -> list[dict]:
    """带缓存的数据获取"""
    if _should_refresh():
        logger.info("缓存过期，重新获取 CBOE VXN 数据...")
        _cache["data"] = _fetch_records()
        _cache["timestamp"] = datetime.utcnow()
    return _cache["data"]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "NasdaqDCA VXN Proxy", "timestamp": datetime.utcnow().isoformat()}


@app.get("/vxn/current")
async def vxn_current():
    """获取最新 VXN 收盘值"""
    records = _get_cached_records()
    latest = records[-1]

    return {
        "symbol": VXN_SYMBOL,
        "date": latest["date"],
        "close": latest["close"],
        "open": latest.get("open"),
        "high": latest.get("high"),
        "low": latest.get("low"),
        "source": "CBOE",
        "cached_at": _cache["timestamp"].isoformat() if _cache["timestamp"] else None,
    }


@app.get("/vxn/history")
async def vxn_history(days: int = Query(default=365, ge=1, le=1825, description="历史天数（最多 5 年）")):
    """获取 VXN 历史收盘数据"""
    records = _get_cached_records()

    cutoff = (datetime.utcnow() - timedelta(days=days)).date()
    records = [r for r in records if r["date"] >= cutoff.isoformat()]

    return {
        "symbol": VXN_SYMBOL,
        "count": len(records),
        "records": records,
        "source": "CBOE",
    }


@app.get("/vxn/range")
async def vxn_range(
    from_date: str = Query(..., description="开始日期 YYYY-MM-DD"),
    to_date: str = Query(..., description="结束日期 YYYY-MM-DD"),
):
    """获取指定日期范围的 VXN 数据"""
    try:
        datetime.strptime(from_date, "%Y-%m-%d")
        datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式错误，请使用 YYYY-MM-DD")

    if from_date > to_date:
        raise HTTPException(status_code=400, detail="from_date 不能晚于 to_date")

    records = _get_cached_records()
    records = [r for r in records if from_date <= r["date"] <= to_date]

    return {
        "symbol": VXN_SYMBOL,
        "from": from_date,
        "to": to_date,
        "count": len(records),
        "records": records,
        "source": "CBOE",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8900, log_level="info")
