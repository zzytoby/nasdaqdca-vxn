"""
Nasdaq-100 DCA Discipline - VXN Data Proxy Backend
====================================================
代理 Yahoo Finance ^VXN 数据，为 iPhone App 提供稳定的 VXN 收盘数据。

Endpoints:
  GET /health              — 健康检查
  GET /vxn/current         — 最新 VXN 收盘值
  GET /vxn/history?days=N  — 历史 VXN 收盘数据（默认 365 天）
  GET /vxn/range?from=YYYY-MM-DD&to=YYYY-MM-DD — 指定日期范围
"""

from datetime import datetime, date, timedelta
from typing import Optional
import logging

import yfinance as yf
import pandas as pd
import pytz
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="NasdaqDCA VXN Proxy",
    description="VXN (Nasdaq-100 Volatility Index) data proxy via Yahoo Finance",
    version="1.0.0",
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
US_EASTERN = pytz.timezone("America/New_York")

# 缓存（避免频繁请求 Yahoo）
_cache: dict = {"data": None, "timestamp": None, "ttl_seconds": 300}


def _should_refresh() -> bool:
    if _cache["data"] is None or _cache["timestamp"] is None:
        return True
    return (datetime.utcnow() - _cache["timestamp"]).total_seconds() > _cache["ttl_seconds"]


def _fetch_vxn_history(period: str = "1y") -> pd.DataFrame:
    """从 Yahoo Finance 获取 VXN 历史数据"""
    ticker = yf.Ticker(VXN_SYMBOL)
    df = ticker.history(period=period)
    if df.empty:
        raise HTTPException(status_code=502, detail="无法从 Yahoo Finance 获取 VXN 数据")
    return df


def _get_cached_or_fetch(period: str = "1y") -> pd.DataFrame:
    """带缓存的数据获取"""
    if _should_refresh():
        logger.info("缓存过期，重新获取 VXN 数据...")
        _cache["data"] = _fetch_vxn_history(period)
        _cache["timestamp"] = datetime.utcnow()
    return _cache["data"]


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame 转 API 响应格式"""
    records = []
    for idx, row in df.iterrows():
        if pd.isna(row.get("Close")):
            continue
        dt = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
        records.append({
            "date": dt.strftime("%Y-%m-%d"),
            "close": round(float(row["Close"]), 2),
            "open": round(float(row.get("Open", 0)), 2) if not pd.isna(row.get("Open")) else None,
            "high": round(float(row.get("High", 0)), 2) if not pd.isna(row.get("High")) else None,
            "low": round(float(row.get("Low", 0)), 2) if not pd.isna(row.get("Low")) else None,
        })
    return records


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "NasdaqDCA VXN Proxy", "timestamp": datetime.utcnow().isoformat()}


@app.get("/vxn/current")
async def vxn_current():
    """获取最新 VXN 收盘值"""
    df = _get_cached_or_fetch("1mo")
    if df.empty:
        raise HTTPException(status_code=502, detail="VXN 数据为空")

    latest = df.iloc[-1]
    latest_date = df.index[-1]
    dt = latest_date.to_pydatetime() if hasattr(latest_date, "to_pydatetime") else latest_date

    return {
        "symbol": VXN_SYMBOL,
        "date": dt.strftime("%Y-%m-%d"),
        "close": round(float(latest["Close"]), 2),
        "open": round(float(latest.get("Open", 0)), 2) if not pd.isna(latest.get("Open")) else None,
        "high": round(float(latest.get("High", 0)), 2) if not pd.isna(latest.get("High")) else None,
        "low": round(float(latest.get("Low", 0)), 2) if not pd.isna(latest.get("Low")) else None,
        "source": "Yahoo Finance",
        "cached_at": _cache["timestamp"].isoformat() if _cache["timestamp"] else None,
    }


@app.get("/vxn/history")
async def vxn_history(days: int = Query(default=365, ge=1, le=1825, description="历史天数（最多 5 年）")):
    """获取 VXN 历史收盘数据"""
    period = "1y" if days <= 365 else ("2y" if days <= 730 else "5y")
    df = _get_cached_or_fetch(period)

    if df.empty:
        raise HTTPException(status_code=502, detail="VXN 数据为空")

    records = _df_to_records(df)
    # 截取最近 N 天
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
    period = "1y" if days_diff <= 365 else ("2y" if days_diff <= 730 else "5y")

    df = _get_cached_or_fetch(period)
    if df.empty:
        raise HTTPException(status_code=502, detail="VXN 数据为空")

    records = _df_to_records(df)
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
