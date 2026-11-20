"""
Nasdaq-100 DCA Discipline - VXN Data Proxy Backend
====================================================
直接调用 Yahoo Finance chart API，为 iPhone App 提供稳定的 VXN 收盘数据。
（不再依赖 yfinance/pandas，改用轻量 httpx 直连，部署更快、更稳）

Endpoints:
  GET /health              — 健康检查
  GET /vxn/current         — 最新 VXN 收盘值
  GET /vxn/history?days=N  — 历史 VXN 收盘数据（默认 365 天）
  GET /vxn/range?from=YYYY-MM-DD&to=YYYY-MM-DD — 指定日期范围
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
import logging

import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="NasdaqDCA VXN Proxy",
    description="VXN (Nasdaq-100 Volatility Index) data proxy via Yahoo Finance",
    version="2.0.0",
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
US_EASTERN = ZoneInfo("America/New_York")

YAHOO_HOSTS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 缓存（避免频繁请求 Yahoo）
_cache: dict = {"data": None, "timestamp": None, "ttl_seconds": 300}


def _should_refresh() -> bool:
    if _cache["data"] is None or _cache["timestamp"] is None:
        return True
    return (datetime.utcnow() - _cache["timestamp"]).total_seconds() > _cache["ttl_seconds"]


def _range_for_days(days: int) -> str:
    """根据天数映射到 Yahoo chart 的 range 参数"""
    if days <= 30:
        return "1mo"
    if days <= 90:
        return "3mo"
    if days <= 180:
        return "6mo"
    if days <= 365:
        return "1y"
    if days <= 730:
        return "2y"
    return "5y"


def _fetch_chart(range_str: str) -> list[dict]:
    """直接调用 Yahoo Finance chart API，返回记录列表"""
    path = f"/v8/finance/chart/{quote(VXN_SYMBOL, safe='')}"
    params = {"range": range_str, "interval": "1d"}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    last_error = None
    for host in YAHOO_HOSTS:
        url = host + path
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=30.0)
            resp.raise_for_status()
            payload = resp.json()

            chart = payload.get("chart", {})
            if chart.get("error"):
                raise RuntimeError(f"Yahoo 返回错误: {chart['error']}")

            results = chart.get("result") or []
            if not results:
                raise RuntimeError("Yahoo 响应为空 result")

            result = results[0]
            timestamps = result.get("timestamp") or []
            quotes = (result.get("indicators", {}).get("quote") or [{}])[0]

            opens = quotes.get("open") or []
            highs = quotes.get("high") or []
            lows = quotes.get("low") or []
            closes = quotes.get("close") or []

            records = []
            for i, ts in enumerate(timestamps):
                close = closes[i] if i < len(closes) else None
                if close is None:
                    continue
                dt = datetime.fromtimestamp(ts, tz=US_EASTERN)
                records.append({
                    "date": dt.strftime("%Y-%m-%d"),
                    "close": round(float(close), 2),
                    "open": round(float(opens[i]), 2) if i < len(opens) and opens[i] is not None else None,
                    "high": round(float(highs[i]), 2) if i < len(highs) and highs[i] is not None else None,
                    "low": round(float(lows[i]), 2) if i < len(lows) and lows[i] is not None else None,
                })

            if not records:
                raise RuntimeError("解析后无有效数据")

            return records
        except Exception as e:  # noqa: BLE001
            last_error = e
            logger.warning("从 %s 获取失败: %s", host, e)
            continue

    raise HTTPException(status_code=502, detail=f"无法从 Yahoo Finance 获取 VXN 数据: {last_error}")


def _get_cached_or_fetch(range_str: str) -> list[dict]:
    """带缓存的数据获取"""
    if _should_refresh():
        logger.info("缓存过期，重新获取 VXN 数据（range=%s）...", range_str)
        _cache["data"] = _fetch_chart(range_str)
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
    records = _get_cached_or_fetch("1mo")
    latest = records[-1]

    return {
        "symbol": VXN_SYMBOL,
        "date": latest["date"],
        "close": latest["close"],
        "open": latest.get("open"),
        "high": latest.get("high"),
        "low": latest.get("low"),
        "source": "Yahoo Finance",
        "cached_at": _cache["timestamp"].isoformat() if _cache["timestamp"] else None,
    }


@app.get("/vxn/history")
async def vxn_history(days: int = Query(default=365, ge=1, le=1825, description="历史天数（最多 5 年）")):
    """获取 VXN 历史收盘数据"""
    range_str = _range_for_days(days)
    records = _get_cached_or_fetch(range_str)

    cutoff = (datetime.utcnow() - timedelta(days=days)).date()
    records = [r for r in records if r["date"] >= cutoff.isoformat()]

    return {
        "symbol": VXN_SYMBOL,
        "count": len(records),
        "records": records,
        "source": "Yahoo Finance",
    }


@app.get("/vxn/range")
async def vxn_range(
    from_date: str = Query(..., description="开始日期 YYYY-MM-DD"),
    to_date: str = Query(..., description="结束日期 YYYY-MM-DD"),
):
    """获取指定日期范围的 VXN 数据"""
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d")
        end = datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式错误，请使用 YYYY-MM-DD")

    days_diff = (end - start).days
    if days_diff < 0:
        raise HTTPException(status_code=400, detail="from_date 不能晚于 to_date")

    range_str = _range_for_days(days_diff + 1)
    records = _get_cached_or_fetch(range_str)
    records = [r for r in records if from_date <= r["date"] <= to_date]

    return {
        "symbol": VXN_SYMBOL,
        "from": from_date,
        "to": to_date,
        "count": len(records),
        "records": records,
        "source": "Yahoo Finance",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8900, log_level="info")
