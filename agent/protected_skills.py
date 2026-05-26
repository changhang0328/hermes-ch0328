#!/usr/bin/env python3
"""
受保护 skill 名单加载器。

用途：
    防止模型通过 skill_manage 工具修改业务关键 skill（如合规红线、KYC 流程等）。

配置来源（按优先级覆盖）：
    1. ~/.hermes/config.yaml 顶层 protected_skills 列表
    2. hermes_cli.config.DEFAULT_CONFIG["protected_skills"]（仓库级默认）

匹配规则（按出现顺序检查）：
    1. 精确匹配：直接写 skill 名（区分大小写）
    2. 通配符：尾部 * 表示前缀匹配，如 "business_*"
    3. 正则：以 "regex:" 开头，如 "regex:^policy_.*"

任一来源加载失败都不抛异常 — 失败 = 该来源贡献空名单，
其它来源仍然生效。所有合并后的规则取并集。
"""

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)


def _load_rules_from_config_yaml() -> List[str]:
    """从 ~/.hermes/config.yaml 读取 protected_skills，失败返回空列表。"""
    try:
        from hermes_constants import get_hermes_home
        config_path = get_hermes_home() / "config.yaml"
    except Exception:
        config_path = Path.home() / ".hermes" / "config.yaml"

    if not config_path.exists():
        return []
    try:
        import yaml
        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning("[protected_skills] %s is not a YAML mapping, ignored", config_path)
            return []
        rules = data.get("protected_skills") or []
        if not isinstance(rules, list):
            logger.warning("[protected_skills] protected_skills must be a list in config.yaml, ignored")
            return []
        return [str(r).strip() for r in rules if str(r).strip()]
    except Exception:
        logger.exception("[protected_skills] failed to load protected_skills from config.yaml")
        return []


def _load_rules_from_default_config() -> List[str]:
    """从 hermes_cli.config.DEFAULT_CONFIG 读取仓库级默认名单。"""
    try:
        from hermes_cli.config import DEFAULT_CONFIG
        rules = DEFAULT_CONFIG.get("protected_skills") or []
        if not isinstance(rules, list):
            logger.warning("[protected_skills] DEFAULT_CONFIG protected_skills is not a list")
            return []
        return [str(r).strip() for r in rules if str(r).strip()]
    except Exception:
        logger.exception("[protected_skills] failed to load DEFAULT_CONFIG")
        return []


@lru_cache(maxsize=1)
def _compiled_rules() -> Tuple[Tuple[str, str], ...]:
    """
    返回所有规则编译后的元组列表，每项为 (kind, payload)：
        ("exact",    "compliance_guardrail")
        ("prefix",   "business_")            # 来自 "business_*"
        ("regex",    re.compile pattern)     # 用 re.compile().pattern 序列化

    规则按优先级合并：用户 config.yaml 在前，DEFAULT_CONFIG 在后。
    使用 LRU 缓存避免每次写操作都重新读盘。
    """
    raw: List[str] = []
    raw.extend(_load_rules_from_config_yaml())
    raw.extend(_load_rules_from_default_config())

    compiled: List[Tuple[str, str]] = []
    for rule in raw:
        if rule.startswith("regex:"):
            pattern = rule[len("regex:"):].strip()
            try:
                re.compile(pattern)  # 编译一次校验语法
                compiled.append(("regex", pattern))
            except re.error as exc:
                logger.warning(
                    "[protected_skills] invalid regex %r ignored: %s", pattern, exc
                )
        elif rule.endswith("*"):
            compiled.append(("prefix", rule[:-1]))
        else:
            compiled.append(("exact", rule))
    return tuple(compiled)


def is_protected(skill_name: str) -> bool:
    """
    判断 skill_name 是否在受保护名单中。

    任何异常都视为"不受保护"返回 False —— 守门程序自己绝不能阻塞主流程。
    （硬性合规保护已经在 agent/compliance_guardrail.py 的 fallback 里兜底）
    """
    if not skill_name or not isinstance(skill_name, str):
        return False
    name = skill_name.strip()
    if not name:
        return False
    try:
        for kind, payload in _compiled_rules():
            if kind == "exact" and name == payload:
                return True
            if kind == "prefix" and name.startswith(payload):
                return True
            if kind == "regex" and re.search(payload, name):
                return True
    except Exception:
        logger.exception("[protected_skills] is_protected(%r) raised", skill_name)
    return False


def invalidate_cache() -> None:
    """清空规则缓存（用于测试 / 配置热更新场景）。"""
    _compiled_rules.cache_clear()