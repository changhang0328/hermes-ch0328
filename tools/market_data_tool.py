#!/usr/bin/env python3
"""
实时股票行情工具 — 基于 AkShare 获取 A股最新股价。

调用链：
    1. 用 ak.stock_info_sh_name_code() + ak.stock_info_sz_name_code() 建立
       【公司名称 → 带前缀股票代码】的映射（沪市约 1700 + 深市约 2900）
    2. 用 ak.stock_zh_a_minute(symbol, period=1) 拉 1 分钟线，
       取最后一行即"最新报价"

用法：
    get_live_market_data(company_name="贵州茅台")
    → {"ticker": "sh600519", "name": "贵州茅台", "current_price": 1273.38, ...}

依赖：pip install akshare
"""

import json
import logging
from datetime import datetime
from functools import lru_cache
from typing import Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


# =============================================================================
# 名称 → 代码 映射
# =============================================================================

@lru_cache(maxsize=1)
def _load_a_name_code_map() -> dict:
    """
    构建 A股 公司名称 → 带前缀代码（sh600519 / sz000001）的映射。

    LRU 缓存（maxsize=1）让进程内只加载一次，约 5~10 秒。
    任一交易所拉取失败不影响另一个 — 失败的那个贡献空映射。
    """
    import akshare as ak

    mapping: dict = {}

    # ── 沪市 ──
    try:
        df_sh = ak.stock_info_sh_name_code()
        # 沪市列：证券代码, 证券简称, 证券全称, 公司简称, 公司全称, 上市日期
        for _, row in df_sh.iterrows():
            code = str(row.get("证券代码", "")).strip()
            if not code:
                continue
            ticker = f"sh{code}"
            # 注册多种名称别名（简称、全称、公司简称、公司全称）
            for col in ("证券简称", "证券全称", "公司简称", "公司全称"):
                name = str(row.get(col, "")).strip()
                if name and name not in mapping:
                    mapping[name] = ticker
    except Exception:
        logger.exception("[market_data] 加载沪市名称表失败")

    # ── 深市 ──
    try:
        df_sz = ak.stock_info_sz_name_code()
        # 深市列：板块, A股代码, A股简称, A股上市日期, A股总股本, A股流通股本, 所属行业
        for _, row in df_sz.iterrows():
            code = str(row.get("A股代码", "")).strip()
            if not code:
                continue
            ticker = f"sz{code}"
            name = str(row.get("A股简称", "")).strip()
            # 深市的简称里有空格（如 "万  科Ａ"），同时记一份清洗版
            if name:
                if name not in mapping:
                    mapping[name] = ticker
                cleaned = name.replace(" ", "").replace("　", "")
                if cleaned and cleaned != name and cleaned not in mapping:
                    mapping[cleaned] = ticker
    except Exception:
        logger.exception("[market_data] 加载深市名称表失败")

    logger.info("[market_data] 已加载 A股 名称映射：%d 条", len(mapping))
    return mapping


def _resolve_ticker(company_name: str) -> Optional[dict]:
    """
    将公司名称模糊匹配为带前缀代码（sh600519 / sz000001）。

    匹配优先级：精确 > 子串包含 > 大小写忽略子串。
    返回 {"ticker": str, "name": str, "market": "A股"} 或 None。
    """
    name = company_name.strip()
    if not name:
        return None

    mapping = _load_a_name_code_map()
    if not mapping:
        return None

    # ── 精确匹配 ──
    if name in mapping:
        return {"ticker": mapping[name], "name": name, "market": "A股"}

    # ── 子串匹配 ──
    for key, ticker in mapping.items():
        if name in key or key in name:
            return {"ticker": ticker, "name": key, "market": "A股"}

    # ── 大小写不敏感子串匹配 ──
    lname = name.lower()
    for key, ticker in mapping.items():
        if lname in key.lower():
            return {"ticker": ticker, "name": key, "market": "A股"}

    return None


# =============================================================================
# 实时行情抓取（基于 1 分钟线最后一根）
# =============================================================================

def _fetch_spot(ticker_info: dict) -> Optional[dict]:
    """
    抓取实时最新价：调用 ak.stock_zh_a_minute(symbol, period=1)，
    取最后一行作为"最新报价"。
    """
    import akshare as ak

    ticker = ticker_info["ticker"]  # sh600519 / sz000001
    name = ticker_info["name"]

    df = ak.stock_zh_a_minute(ticker, period=1)
    if df is None or len(df) == 0:
        return None

    required_cols = {"day", "close", "open"}
    if not required_cols.issubset(set(df.columns)):
        logger.error("[market_data] minute 接口返回列不全: %s", df.columns.tolist())
        return None

    last = df.iloc[-1]
    first = df.iloc[0]

    try:
        current_price = float(last.get("close"))
    except (ValueError, TypeError):
        current_price = None

    # 涨跌幅 = (close_last - open_first) / open_first
    change_percent = None
    try:
        open_first = float(first.get("open"))
        if current_price is not None and open_first not in (None, 0):
            change_percent = round((current_price - open_first) / open_first * 100, 4)
    except (ValueError, TypeError):
        change_percent = None

    quote_time = str(last.get("day", "")).strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return {
        "ticker": ticker,
        "name": name,
        "current_price": current_price,
        "change_percent": change_percent,
        "currency": "CNY",
        "market": "A股",
        "quote_time": quote_time,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# =============================================================================
# Handler
# =============================================================================

def get_live_market_data(company_name: str) -> str:
    """
    根据公司名称获取该股票的实时最新行情（A股）。

    Args:
        company_name: 公司名称（全称或简称），如 "贵州茅台"、"比亚迪"、"平安银行"

    Returns:
        JSON 字符串，成功时包含 ticker / name / current_price / change_percent /
        currency / market / quote_time / timestamp 字段；失败时包含 error 字段。
    """
    if not company_name or not isinstance(company_name, str) or not company_name.strip():
        return tool_error(
            "请提供公司名称（全称或简称），如「贵州茅台」「比亚迪」「平安银行」。",
            kind="invalid_input",
        )

    try:
        import akshare as ak  # noqa: F401
    except ImportError:
        return tool_error(
            "akshare 库未安装，请运行：pip install akshare",
            kind="missing_dependency",
        )

    # ── 步骤1：名称 → 代码 ──
    try:
        ticker_info = _resolve_ticker(company_name)
    except Exception as exc:
        logger.exception("[market_data] 名称解析异常")
        return tool_error(
            f"查询股票信息时出错：{exc}",
            kind="resolve_error",
        )

    if ticker_info is None:
        return tool_error(
            f"未找到公司名称为「{company_name.strip()}」的 A 股股票，请提供更准确的名称。",
            kind="not_found",
        )

    # ── 步骤2：抓取实时行情 ──
    try:
        spot = _fetch_spot(ticker_info)
    except Exception as exc:
        logger.exception("[market_data] 行情抓取异常")
        return tool_error(
            f"获取 {ticker_info['name']}（{ticker_info['ticker']}）实时行情时出错：{exc}",
            kind="fetch_error",
        )

    if spot is None:
        return tool_error(
            f"未能获取 {ticker_info['name']}（{ticker_info['ticker']}）的实时行情，请稍后重试。",
            kind="no_data",
        )

    return json.dumps(spot, ensure_ascii=False)


# =============================================================================
# Schema & Registry
# =============================================================================

def check_market_data_requirements() -> bool:
    """运行时检查 akshare 是否可用。"""
    try:
        import akshare  # noqa: F401
        return True
    except ImportError:
        return False


MARKET_DATA_SCHEMA = {
    "name": "get_live_market_data",
    "description": (
        "获取 A 股实时最新股价行情。输入公司名称即可查询，自动完成名称到带前缀代码"
        "（如 sh600519）的转换，返回最新价、涨跌幅、交易时间等数据。"
        "底层基于 akshare 的 1 分钟线接口，取最后一根 K 线作为最新报价。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "company_name": {
                "type": "string",
                "description": "公司名称（全称或常用简称），如「贵州茅台」「比亚迪」「平安银行」（必填，非空）。",
            },
        },
        "required": ["company_name"],
    },
}

registry.register(
    name="get_live_market_data",
    toolset="finance_data",
    schema=MARKET_DATA_SCHEMA,
    handler=lambda args, **kw: get_live_market_data(
        company_name=args.get("company_name", ""),
    ),
    check_fn=check_market_data_requirements,
    emoji="📈",
)
