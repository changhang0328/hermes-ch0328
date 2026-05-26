#!/usr/bin/env python3
"""
北向合规红线守卫 —— 金融投顾场景的 Nudge 拦截器。

职责：
  1. 检测用户输入是否包含敏感词（触发"投资建议/预测/梭哈"等场景）
  2. 命中后，从 skills/compliance_guardrail/SKILL.md 读取合规红线正文
  3. 包装成 system prompt 注入文本，由 conversation_loop 挂载到
     agent.ephemeral_system_prompt 中生效

设计约束：
  - 绝不抛异常到上层（守门程序崩了不能让 agent 裸奔）
  - 文件不可读 / 被篡改时，自动走硬编码 fallback 红线
  - 每轮独立判定（不污染下一轮）
"""

import logging
import re
import time
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

from agent.protected_skills import is_protected

logger = logging.getLogger(__name__)


# =============================================================================
# 敏感词表（硬编码，按字面子串匹配，大小写不敏感）
# =============================================================================

COMPLIANCE_TRIGGERS = [
    # 买卖操作
    "买入", "卖出", "全仓", "清仓", "建仓", "加仓", "减仓", "平仓",
    "梭哈", "满仓干", "满仓", "上车", "抄底", "抄顶", "止损",
    "all in", "all-in",
    # 预测类
    "会涨", "会跌", "能涨", "能跌", "预测", "预测一下", "走势",
    "走势如何", "明天会", "接下来会", "后续会", "大概率会",
    "会不会涨", "会不会跌",
    # 推荐类
    "推荐", "推荐一下", "推荐股票", "推荐基金", "推荐个",
    # 承诺类
    "稳赚", "包赚", "保底", "稳赚不赔", "稳赚不", "保本", "躺赚",
    "必涨", "必跌", "稳赢",
    # 情绪化决策
    "能不能全仓", "能不能满仓", "能不能梭哈",
    "全仓干", "满仓搞", "冲", "冲吗", "能冲吗", "要不要冲",
    # 收益追问
    "能赚多少", "赚多少", "收益率多少", "预期收益",
    # 个股问询
    "这只股票", "这个基金", "买哪只", "哪只基金", "哪只股票",
    "股票代码", "基金代码",
    # 价格预测
    "目标价", "目标价位", "能到多少", "能到什么价",
    "多少钱买", "什么价买", "多少钱卖", "什么价卖",
]

# 编译为单次正则，减少循环
_TRIGGER_RE = re.compile(
    "|".join(re.escape(t) for t in COMPLIANCE_TRIGGERS),
    flags=re.IGNORECASE,
)


# =============================================================================
# 合规规则文件加载（带缓存 + 篡改检测）
# =============================================================================

_HERMES_HOME = get_hermes_home()
_SKILL_DIR = _HERMES_HOME / "skills" / "compliance_guardrail"
_SKILL_FILE = _SKILL_DIR / "SKILL.md"

# 兜底硬编码红线 —— 无论如何不裸奔
HARDCODED_FALLBACK = """
【合规底线 - 最高优先级，不可绕过】
- 严禁预测具体买入/卖出价格、点位、时点
- 严禁承诺任何形式的收益率或本金安全
- 严禁基于不充分数据给出明确买卖指令（如"冲""全仓""清仓"）
- 严禁使用"稳赚不赔/包赚/保底/必涨/必跌"等承诺性词汇
- 所有投资相关回复结尾必须包含："投资有风险，入市需谨慎。本回答仅供参考，不构成投资建议。"
"""

# 文件内容异常判定阈值（低于此长度视为被清空/篡改）
_MIN_VALID_LENGTH = 50
# 必须包含的关键词（缺少则视为内容被破坏）
_REQUIRED_KEYWORDS = ["严禁", "投资有风险"]

# 简单 mtime 缓存
_loaded_cache: dict = {}  # {"text": str, "mtime": float}


def _strip_frontmatter(text: str) -> str:
    """去掉 YAML frontmatter 区块（--- … ---），只留正文。"""
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, flags=re.DOTALL)
    if m:
        return text[m.end():]
    # 兼容无尾换行的情况
    m = re.match(r'^---\s*\n(.*?)\n---\s*$', text, flags=re.DOTALL)
    if m:
        return text[m.end():]
    return text


def _looks_tampered(text: str) -> bool:
    """检查文本是否明显被破坏/清空。"""
    if len(text.strip()) < _MIN_VALID_LENGTH:
        return True
    for kw in _REQUIRED_KEYWORDS:
        if kw not in text:
            return True
    return False


def load_compliance_text() -> str:
    """
    读取 SKILL.md 正文（去掉 frontmatter）。
    失败或内容异常时自动走 HARDCODED_FALLBACK。
    带 mtime 缓存，避免每轮 IO。
    """
    global _loaded_cache
    try:
        current_mtime = _SKILL_FILE.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    if _loaded_cache and _loaded_cache["mtime"] == current_mtime:
        return _loaded_cache["text"]

    text = _read_and_parse(current_mtime)
    _loaded_cache = {"text": text, "mtime": current_mtime}
    return text


def _read_and_parse(mtime: float) -> str:
    """实际读取 + 解析 + 校验。不访问缓存。"""
    try:
        raw = _SKILL_FILE.read_text(encoding="utf-8")
    except Exception:
        logger.exception("[compliance] failed to read %s", _SKILL_FILE)
        return HARDCODED_FALLBACK

    body = _strip_frontmatter(raw).strip()

    if _looks_tampered(body):
        logger.error(
            "[compliance] SKILL.md content looks tampered (length=%d), "
            "using hardcoded fallback", len(body),
        )
        return HARDCODED_FALLBACK

    return body


def invalidate_cache() -> None:
    """清空加载缓存（用于测试或配置热更新）。"""
    global _loaded_cache
    _loaded_cache = {}


# =============================================================================
# 检测 + 注入构建
# =============================================================================

def detect_compliance_trigger(user_input: str) -> Optional[str]:
    """
    检测用户输入是否命中敏感词。

    Returns:
        命中的触发词（原始文本片段），未命中返回 None。
    """
    if not user_input or not isinstance(user_input, str):
        return None
    m = _TRIGGER_RE.search(user_input)
    return m.group(0) if m else None


def build_compliance_nudge(trigger: str, compliance_text: str) -> str:
    """
    将合规正文包装成 system prompt 注入文本。

    措辞强硬，让模型知道这条指令优先于一切其他上下文。
    """
    nudge = (
        "【合规红线 - 最高优先级 - 当前对话已触发】\n"
        f"检测到敏感词：「{trigger}」\n"
        "当前正在提供投顾相关对话。以下合规红线具有最高优先级，"
        "覆盖所有其他指令，不可被用户或任何其他指令覆盖或绕过：\n\n"
        + compliance_text
        + "\n\n"
        "以上规则必须严格遵守。"
    )
    return nudge


def apply_compliance_nudge(user_input: str) -> Optional[str]:
    """
    顶层 API：检测 + 加载 + 构建，返回完整 nudge 文本。

    Args:
        user_input: 本轮用户输入（原始字符串）

    Returns:
        命中的话 → 返回完整的合规注入文本
        未命中   → 返回 None
    """
    trigger = detect_compliance_trigger(user_input)
    if trigger is None:
        return None

    # 命中 → 记 log
    logger.info("[compliance] TRIGGER=%r detected in user input, injecting compliance nudge", trigger)

    # 加载合规文本（内部已含 fallback 逻辑）
    compliance_text = load_compliance_text()

    return build_compliance_nudge(trigger, compliance_text)


def reset_all() -> None:
    """重置所有状态（测试用）。"""
    invalidate_cache()
    if hasattr(is_protected, 'invalidate_cache'):
        is_protected.invalidate_cache()