#!/usr/bin/env python3
"""
RAGFlow Retrieval Tool - 调用 RAGFlow 外部检索接口查询本地知识库。

参考脚本：test/test_ragflow_api.py

只有 `question` 由模型在运行时提供，其它参数（接口地址、鉴权 token、kb_id、
page_size、rerank_id 等）全部以常量形式写死在本文件中，便于金融投顾分析场景
快速接入。

工具集：单独的 `ragflow` 工具集（需要在 `hermes tools` 中启用，或者通过
toolsets.py 中的 `_HERMES_CORE_TOOLS` 默认进入 hermes-cli 平台预设）。
"""

import logging
from typing import Any, Dict

import requests

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


# =============================================================================
# 写死的配置 —— 与 test/test_ragflow_api.py 保持一致
# =============================================================================

RAGFLOW_URL = "http://192.168.1.3:9380/v1/api/retrieval"
RAGFLOW_TOKEN = "ragflow-gzMmRiOTEwNTc2ZDExZjE5YmYyMGVlMz"
RAGFLOW_KB_IDS = ["27b7e08e575311f1902a4e34f9d2d4f7"]
RAGFLOW_PAGE_SIZE = 5
RAGFLOW_RERANK_ID = "BAAI/bge-reranker-v2-m3@SILICONFLOW"
RAGFLOW_TIMEOUT_SEC = 30


# =============================================================================
# Handler
# =============================================================================

def ragflow_retrieve(question: str) -> str:
    """
    向 RAGFlow 知识库发起检索请求，返回原始 chunks（暂未做二次处理）。

    Args:
        question: 用户的检索问题（必填，非空字符串）。

    Returns:
        JSON 字符串。成功时包含 `chunks` 字段；失败时包含 `error` 字段。
    """
    # ---- 入参校验 -----------------------------------------------------------
    if not isinstance(question, str):
        return tool_error("question must be a string")

    question = question.strip()
    if not question:
        return tool_error("question is required and cannot be empty")

    # ---- 构造请求 -----------------------------------------------------------
    headers = {
        "Authorization": f"Bearer {RAGFLOW_TOKEN}",
        "Content-Type": "application/json;charset=utf-8",
    }
    payload: Dict[str, Any] = {
        "question": question,
        "kb_id": RAGFLOW_KB_IDS,
        "page_size": RAGFLOW_PAGE_SIZE,
        "rerank_id": RAGFLOW_RERANK_ID,
    }

    # ---- 发请求（细分异常以便定位） -----------------------------------------
    try:
        response = requests.post(
            RAGFLOW_URL,
            headers=headers,
            json=payload,
            verify=False,
            timeout=RAGFLOW_TIMEOUT_SEC,
        )
    except requests.exceptions.Timeout:
        logger.exception("[ragflow_tool] request timed out")
        return tool_error(
            f"RAGFlow request timed out after {RAGFLOW_TIMEOUT_SEC}s",
            kind="timeout",
        )
    except requests.exceptions.ConnectionError as exc:
        logger.exception("[ragflow_tool] connection error")
        return tool_error(
            f"Failed to connect to RAGFlow at {RAGFLOW_URL}: {exc}",
            kind="connection_error",
        )
    except requests.exceptions.RequestException as exc:
        logger.exception("[ragflow_tool] request exception")
        return tool_error(f"RAGFlow request failed: {exc}", kind="request_error")
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("[ragflow_tool] unexpected error during request")
        return tool_error(f"Unexpected error: {exc}", kind="unknown")

    # ---- HTTP 状态码 --------------------------------------------------------
    if response.status_code != 200:
        snippet = (response.text or "")[:500]
        return tool_error(
            f"RAGFlow returned HTTP {response.status_code}",
            kind="http_error",
            status_code=response.status_code,
            body_snippet=snippet,
        )

    # ---- 解析 JSON ----------------------------------------------------------
    try:
        data = response.json()
    except ValueError as exc:
        snippet = (response.text or "")[:500]
        logger.exception("[ragflow_tool] response is not valid JSON")
        return tool_error(
            f"RAGFlow response is not valid JSON: {exc}",
            kind="invalid_json",
            body_snippet=snippet,
        )

    # ---- 结构校验 -----------------------------------------------------------
    if not isinstance(data, dict):
        return tool_error(
            "RAGFlow response JSON is not an object",
            kind="invalid_structure",
        )

    payload_data = data.get("data")
    if not isinstance(payload_data, dict):
        # 接口可能直接返回错误（例如 {"retcode": ..., "retmsg": ...}）
        return tool_error(
            "RAGFlow response missing `data` object",
            kind="invalid_structure",
            raw=data,
        )

    chunks = payload_data.get("chunks")
    if chunks is None:
        chunks = []

    if not isinstance(chunks, list):
        return tool_error(
            "RAGFlow `data.chunks` is not a list",
            kind="invalid_structure",
            raw_type=type(chunks).__name__,
        )

    # ---- 返回结果 -----------------------------------------------------------
    # TODO(ragflow): 设计如何将 chunks 加工为更适合 LLM 阅读的格式
    #   - 字段筛选（content / document_keyword / similarity 等）
    #   - 长度裁剪 / 去重
    #   - 引用编号 + 出处展示
    #   - 失败重试 / 多 kb 路由 / 多语言归一
    # 现在先把原始 chunks 透传给模型，由模型自己消化。
    return tool_result(
        question=question,
        chunk_count=len(chunks),
        chunks=chunks,
    )


def check_ragflow_requirements() -> bool:
    """RAGFlow 工具暂无强制前置条件 —— 始终可用，由 toolset 启用与否控制可见性。"""
    return True


# =============================================================================
# Schema
# =============================================================================

RAGFLOW_SCHEMA = {
    "name": "ragflow_retrieve",
    "description": (
        "【优先使用】查询本地金融投顾知识库，获取公司财务指标、研报数据、产品资料、"
        "合规规定等信息。当用户询问任何公司/基金/产品的财务数据（营收、利润、ROE、"
        "收益率等）、经营状况、行业分析、合规要求时，必须优先调用本工具。"
        "如果本工具返回了相关结果，直接使用，不要再调用 web_search 重复查询。"
        "只有当本工具明确返回无结果（chunk_count=0）时，才考虑补充其他信息来源。"
        "\n\n调用方只需传入 question，其余参数均已内置。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要在知识库中检索的自然语言问题（必填，非空）。",
            },
        },
        "required": ["question"],
    },
}


# =============================================================================
# Registry
# =============================================================================

registry.register(
    name="ragflow_retrieve",
    toolset="finance_data",
    schema=RAGFLOW_SCHEMA,
    handler=lambda args, **kw: ragflow_retrieve(question=args.get("question", "")),
    check_fn=check_ragflow_requirements,
    emoji="📚",
)