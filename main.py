"""
Nasdaq-100 DCA Discipline - VXN + NDX Data Proxy Backend
=========================================================
数据源：
  VXN: CBOE 官方  https://cdn.cboe.com/api/global/us_indices/daily_prices/VXN_History.csv
  NDX: NASDAQ 官方 https://api.nasdaq.com/api/quote/NDX/historical (用于计算回撤)

规则版本：v4.0 (2026-11-21)

Endpoints:
  GET /health              — 健康检查
  GET /vxn/current         — 最新 VXN 收盘值
  GET /vxn/history?days=N  — 历史 VXN 收盘数据
  GET /vxn/range?from=YYYY-MM-DD&to=YYYY-MM-DD
  GET /vxn/drawdown        — 当前回撤数据（52周滚动最高点）
  GET /vxn/tier            — 当前档位判断 + 判断依据
"""

from datetime import datetime, timedelta, date
import csv
import io
import json
import logging
import os
import time as _time

import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="NasdaqDCA VXN + Tier Proxy",
    description="VXN data + NDX drawdown + tier determination for Nasdaq-100 DCA discipline",
    version="4.0.0",
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
NDX_SYMBOL = "^NDX"

CBOE_VXN_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VXN_History.csv"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 缓存 TTL
CACHE_TTL = 600  # 10 分钟

# 冷静期状态文件路径
COOLDOWN_STATE_FILE = os.path.join(os.path.dirname(__file__), "cooldown_state.json")

# ---------------------------------------------------------------------------
# Tier v4.0 规则常量
# ---------------------------------------------------------------------------
# 第一档进入
TIER1_ENTRY_DRAWDOWN = 10.0         # 回撤 ≥ 10%
# 第一档退出
TIER1_EXIT_DRAWDOWN = 5.0           # 路径 A: 回撤 ≤ 5%
TIER1_EXIT_VXN = 30.0               # 路径 B: VXN ≤ 30
TIER1_EXIT_VXN_DAYS = 3             # 路径 B: 连续 3 天
# 冷静期
COOLDOWN_DAYS = 5                   # 退出后 5 个交易日
# 短期再入
REENTRY_WINDOW_DAYS = 20            # 20 个交易日内
REENTRY_DRAWDOWN_HIGHER = 12.0      # 门槛提高到 12%
# 第三档
TIER3_ENTRY_VXN = 25.0              # VXN < 25 连续 3 天
TIER3_ENTRY_DAYS = 3
TIER3_EXIT_VXN = 27.0               # VXN > 27 连续 3 天
TIER3_EXIT_DAYS = 3

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_cache: dict = {
    "vxn": {"data": None, "ts": None},
    "ndx": {"data": None, "ts": None},
}


def _is_stale(cache_key: str) -> bool:
    entry = _cache[cache_key]
    if entry["data"] is None or entry["ts"] is None:
        return True
    return (datetime.utcnow() - entry["ts"]).total_seconds() > CACHE_TTL


# ---------------------------------------------------------------------------
# VXN data (CBOE)
# ---------------------------------------------------------------------------

def _fetch_vxn() -> list[dict]:
    """从 CBOE 拉取 VXN 全量历史，返回记录列表（日期升序）"""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/csv,*/*"}
    resp = httpx.get(CBOE_VXN_URL, headers=headers, timeout=30.0, follow_redirects=True)
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
        raise HTTPException(status_code=502, detail="CBOE VXN 数据为空或解析失败")

    return records


def _get_vxn() -> list[dict]:
    if _is_stale("vxn"):
        logger.info("缓存过期，重新获取 CBOE VXN...")
        _cache["vxn"]["data"] = _fetch_vxn()
        _cache["vxn"]["ts"] = datetime.utcnow()
    return _cache["vxn"]["data"]


# ---------------------------------------------------------------------------
# NDX data (NASDAQ 官方 API)
# ---------------------------------------------------------------------------

NASDAQ_NDX_URL = "https://api.nasdaq.com/api/quote/NDX/historical"


def _parse_nasdaq_price(raw: str) -> float:
    """解析 NASDAQ API 价格字符串，如 '$25,028.98' → 25028.98"""
    return float(raw.replace("$", "").replace(",", ""))


def _fetch_ndx() -> list[dict]:
    """
    从 NASDAQ 官方 API 拉取 NDX 最近 2 年日线数据。
    返回记录列表（日期升序），每项含 date / close / open / high / low。
    """
    today = date.today()
    two_years_ago = today - timedelta(days=730)

    params = {
        "assetclass": "index",
        "fromdate": two_years_ago.strftime("%Y-%m-%d"),
        "limit": 520,  # ~2 年交易日
        "todate": today.strftime("%Y-%m-%d"),
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://www.nasdaq.com/",
    }

    logger.info("从 NASDAQ 官方 API 获取 NDX 数据...")
    resp = httpx.get(NASDAQ_NDX_URL, params=params, headers=headers, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()

    body = resp.json()
    if body.get("status", {}).get("rCode") != 200:
        raise HTTPException(status_code=502, detail=f"NASDAQ API 返回异常: {body}")

    rows = body.get("data", {}).get("tradesTable", {}).get("rows", [])
    if not rows:
        raise HTTPException(status_code=502, detail="NASDAQ NDX 数据为空")

    records = []
    for row in rows:
        try:
            close = _parse_nasdaq_price(row["close"])
            if close <= 0:
                continue
            # NASDAQ API 日期格式: "MM/DD/YYYY"
            dt = datetime.strptime(row["date"], "%m/%d/%Y")
            records.append({
                "date": dt.strftime("%Y-%m-%d"),
                "close": close,
                "open": _parse_nasdaq_price(row["open"]) if row.get("open") and row["open"] != "N/A" else None,
                "high": _parse_nasdaq_price(row["high"]) if row.get("high") and row["high"] != "N/A" else None,
                "low": _parse_nasdaq_price(row["low"]) if row.get("low") and row["low"] != "N/A" else None,
            })
        except (ValueError, KeyError, TypeError) as e:
            logger.warning(f"跳过无法解析的 NDX 记录: {row.get('date', '?')} — {e}")
            continue

    if not records:
        raise HTTPException(status_code=502, detail="NASDAQ NDX 数据解析后为空")

    # 按日期升序排列（NASDAQ API 返回降序）
    records.sort(key=lambda r: r["date"])

    logger.info(f"NDX 数据获取成功: {len(records)} 条记录 ({records[0]['date']} ~ {records[-1]['date']})")
    return records


def _get_ndx() -> list[dict]:
    if _is_stale("ndx"):
        logger.info("缓存过期，重新获取 NDX 数据...")
        _cache["ndx"]["data"] = _fetch_ndx()
        _cache["ndx"]["ts"] = datetime.utcnow()
    return _cache["ndx"]["data"]


# ---------------------------------------------------------------------------
# Drawdown calculation
# ---------------------------------------------------------------------------

def _calculate_drawdown(ndx_records: list[dict]) -> dict:
    """
    计算滚动 52 周（365 天）回撤。
    返回:
      - current_price: 最新收盘价
      - high_52w: 52 周最高收盘价
      - high_52w_date: 最高点日期
      - drawdown_pct: 回撤百分比（正数表示下跌）
    """
    today = date.today()
    cutoff = today - timedelta(days=365)

    # 筛选 52 周内的数据
    window = [r for r in ndx_records if r["date"] >= cutoff.isoformat()]
    if not window:
        raise HTTPException(status_code=502, detail="52 周窗口内无 NDX 数据")

    high_record = max(window, key=lambda r: r["close"])
    current = window[-1]

    drawdown = (current["close"] - high_record["close"]) / high_record["close"] * 100

    return {
        "current_price": current["close"],
        "current_date": current["date"],
        "high_52w": high_record["close"],
        "high_52w_date": high_record["date"],
        "drawdown_pct": round(drawdown, 2),  # 负数 = 下跌
        "drawdown_abs": round(abs(drawdown), 2),  # 正数绝对值
    }


# ---------------------------------------------------------------------------
# Consecutive days helper
# ---------------------------------------------------------------------------

def _consecutive_above(records: list[dict], threshold: float, days: int) -> bool:
    """检查最近 N 天是否连续高于阈值"""
    if len(records) < days:
        return False
    recent = records[-days:]
    return all(r["close"] > threshold for r in recent)


def _consecutive_below(records: list[dict], threshold: float, days: int) -> bool:
    """检查最近 N 天是否连续低于阈值"""
    if len(records) < days:
        return False
    recent = records[-days:]
    return all(r["close"] < threshold for r in recent)


def _count_consecutive_below(records: list[dict], threshold: float) -> int:
    """从最新一天往回数，连续低于阈值的交易天数"""
    count = 0
    for r in reversed(records):
        if r["close"] <= threshold:
            count += 1
        else:
            break
    return count


# ---------------------------------------------------------------------------
# Cooldown state persistence
# ---------------------------------------------------------------------------

def _load_cooldown_state() -> dict:
    """加载冷静期状态"""
    default = {
        "in_cooldown": False,
        "cooldown_start_date": None,    # 冷静期开始的日期
        "cooldown_start_trading_day": 0, # 冷静期开始时的交易天数计数
        "last_tier1_exit_date": None,    # 上次退出第一档的日期
        "last_tier1_exit_trading_day": 0,
    }
    try:
        if os.path.exists(COOLDOWN_STATE_FILE):
            with open(COOLDOWN_STATE_FILE, "r") as f:
                saved = json.load(f)
            default.update(saved)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"读取冷静期状态文件失败: {e}")
    return default


def _save_cooldown_state(state: dict):
    """持久化冷静期状态"""
    try:
        with open(COOLDOWN_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except IOError as e:
        logger.error(f"保存冷静期状态失败: {e}")


# ---------------------------------------------------------------------------
# Tier determination (v4.0)
# ---------------------------------------------------------------------------

def _determine_tier(vxn_records: list[dict], ndx_records: list[dict]) -> dict:
    """
    根据 v4.0 规则判断当前档位。
    返回完整判断结果，包含所有依据。
    """
    dd = _calculate_drawdown(ndx_records)
    latest_vxn = vxn_records[-1]["close"]
    latest_date = vxn_records[-1]["date"]

    cooldown_state = _load_cooldown_state()

    drawdown_abs = dd["drawdown_abs"]

    # 统计 VXN 连续低于各阈值的天数
    vxn_below_30_days = _count_consecutive_below(vxn_records, TIER1_EXIT_VXN)
    vxn_below_25_days = _count_consecutive_below(vxn_records, TIER3_ENTRY_VXN)
    vxn_above_27_days = 0
    # 从最新往回数连续 > 27 的天数
    for r in reversed(vxn_records):
        if r["close"] > TIER3_EXIT_VXN:
            vxn_above_27_days += 1
        else:
            break

    # ── 判断当前是否处于冷静期 ──
    in_cooldown = False
    trading_days_since_exit = 999  # 默认很大

    if cooldown_state["last_tier1_exit_date"]:
        # 计算从退出日到最新日之间有多少个交易日的 NDX 数据
        exit_date = cooldown_state["last_tier1_exit_date"]
        trading_days_after_exit = [
            r for r in ndx_records if r["date"] > exit_date
        ]
        trading_days_since_exit = len(trading_days_after_exit)

        if trading_days_since_exit < COOLDOWN_DAYS:
            in_cooldown = True
        else:
            in_cooldown = False

    # ── 确定当前在第三档还是其他档 ──
    # 需要追踪当前档位：从 cooldown state 里读上一次判断的档位
    current_tier = cooldown_state.get("current_tier", "tier2")

    # ── 第三档判断（不受冷静期影响） ──
    if vxn_below_25_days >= TIER3_ENTRY_DAYS:
        # 检查是否应该从第三档退出
        if current_tier == "tier3" and vxn_above_27_days >= TIER3_EXIT_DAYS:
            tier = "tier2"
            reason = f"VXN > 27 连续 {vxn_above_27_days} 天，退出第三档"
        else:
            tier = "tier3"
            reason = f"VXN < 25 连续 {vxn_below_25_days} 天，进入第三档"
    else:
        # 不在第三档条件 → 看第一档和第二档

        # ── 第一档退出条件检查（如果当前在第一档） ──
        if current_tier == "tier1":
            # 路径 A: 回撤恢复
            if drawdown_abs <= TIER1_EXIT_DRAWDOWN:
                tier = "tier2"
                reason = f"回撤 {drawdown_abs}% ≤ {TIER1_EXIT_DRAWDOWN}%，路径 A 退出第一档"
                # 记录退出时间，触发冷静期
                cooldown_state["last_tier1_exit_date"] = latest_date
                cooldown_state["in_cooldown"] = True
                _save_cooldown_state(cooldown_state)
            # 路径 B: VXN 回落
            elif vxn_below_30_days >= TIER1_EXIT_VXN_DAYS:
                tier = "tier2"
                reason = f"VXN ≤ 30 连续 {vxn_below_30_days} 天，路径 B 退出第一档"
                cooldown_state["last_tier1_exit_date"] = latest_date
                cooldown_state["in_cooldown"] = True
                _save_cooldown_state(cooldown_state)
            else:
                tier = "tier1"
                reason = f"维持第一档（退出条件未满足：回撤 {drawdown_abs}% > {TIER1_EXIT_DRAWDOWN}%，VXN≤30 连续 {vxn_below_30_days}/{TIER1_EXIT_VXN_DAYS} 天）"

        else:
            # 当前不在第一档 → 检查是否应该进入第一档
            if in_cooldown:
                tier = "tier2"
                reason = f"冷静期中（退出后 {trading_days_since_exit}/{COOLDOWN_DAYS} 天），忽略第一档进入条件"
            elif trading_days_since_exit <= REENTRY_WINDOW_DAYS and cooldown_state["last_tier1_exit_date"]:
                # 短期再入：门槛提高到 12%
                if drawdown_abs >= REENTRY_DRAWDOWN_HIGHER:
                    tier = "tier1"
                    reason = f"回撤 {drawdown_abs}% ≥ {REENTRY_DRAWDOWN_HIGHER}%（短期再入门槛），进入第一档"
                else:
                    tier = "tier2"
                    reason = f"回撤 {drawdown_abs}% < {REENTRY_DRAWDOWN_HIGHER}%（短期再入门槛），维持第二档"
            else:
                # 正常进入条件：回撤 ≥ 10%
                if drawdown_abs >= TIER1_ENTRY_DRAWDOWN:
                    tier = "tier1"
                    reason = f"回撤 {drawdown_abs}% ≥ {TIER1_ENTRY_DRAWDOWN}%，进入第一档"
                else:
                    tier = "tier2"
                    reason = f"回撤 {drawdown_abs}% < {TIER1_ENTRY_DRAWDOWN}%，维持第二档"

    # ── 持久化当前状态 ──
    cooldown_state["current_tier"] = tier
    cooldown_state["in_cooldown"] = in_cooldown
    cooldown_state["last_updated"] = datetime.utcnow().isoformat()
    _save_cooldown_state(cooldown_state)

    return {
        "tier": tier,
        "tier_label": {
            "tier1": "第一档｜极度恐慌",
            "tier2": "第二档｜正常市场",
            "tier3": "第三档｜低波动",
        }[tier],
        "reason": reason,
        "vxn": {
            "latest": latest_vxn,
            "date": latest_date,
            "consecutive_below_30": vxn_below_30_days,
            "consecutive_below_25": vxn_below_25_days,
            "consecutive_above_27": vxn_above_27_days,
        },
        "drawdown": dd,
        "cooldown": {
            "in_cooldown": in_cooldown,
            "trading_days_since_exit": trading_days_since_exit,
            "cooldown_required_days": COOLDOWN_DAYS,
            "last_tier1_exit_date": cooldown_state["last_tier1_exit_date"],
        },
        "reentry": {
            "within_window": trading_days_since_exit <= REENTRY_WINDOW_DAYS,
            "window_days": REENTRY_WINDOW_DAYS,
            "normal_threshold": TIER1_ENTRY_DRAWDOWN,
            "elevated_threshold": REENTRY_DRAWDOWN_HIGHER if trading_days_since_exit <= REENTRY_WINDOW_DAYS else None,
        },
        "rules_version": "4.0.0",
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "NasdaqDCA VXN + Tier Proxy",
        "version": "4.0.0",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/vxn/current")
async def vxn_current():
    """获取最新 VXN 收盘值"""
    records = _get_vxn()
    latest = records[-1]
    return {
        "symbol": VXN_SYMBOL,
        "date": latest["date"],
        "close": latest["close"],
        "open": latest.get("open"),
        "high": latest.get("high"),
        "low": latest.get("low"),
        "source": "CBOE",
        "cached_at": _cache["vxn"]["ts"].isoformat() if _cache["vxn"]["ts"] else None,
    }


@app.get("/vxn/history")
async def vxn_history(days: int = Query(default=365, ge=1, le=1825)):
    """获取 VXN 历史收盘数据"""
    records = _get_vxn()
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

    records = _get_vxn()
    records = [r for r in records if from_date <= r["date"] <= to_date]
    return {
        "symbol": VXN_SYMBOL,
        "from": from_date,
        "to": to_date,
        "count": len(records),
        "records": records,
        "source": "CBOE",
    }


@app.get("/vxn/drawdown")
async def vxn_drawdown():
    """
    获取 NDX 当前回撤数据。
    返回滚动 52 周最高点、当前价格、回撤百分比。
    """
    ndx = _get_ndx()
    dd = _calculate_drawdown(ndx)
    return {
        "symbol": NDX_SYMBOL,
        **dd,
        "source": "NASDAQ",
        "cached_at": _cache["ndx"]["ts"].isoformat() if _cache["ndx"]["ts"] else None,
    }


@app.get("/vxn/tier")
async def vxn_tier():
    """
    获取当前定投档位判断结果。
    返回档位、标签、判断依据、VXN 状态、回撤数据、冷静期状态。
    """
    vxn = _get_vxn()
    ndx = _get_ndx()
    result = _determine_tier(vxn, ndx)
    result["timestamp"] = datetime.utcnow().isoformat()
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8900, log_level="info")
