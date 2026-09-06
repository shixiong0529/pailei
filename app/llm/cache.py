"""模型结果持久化缓存。

缓存键覆盖：供应商接口标识、模型名、thinking/reasoning 配置、temperature、max tokens、
系统提示词与用户提示词的完整哈希、提示词版本、业务步骤名称、校验版本。
用户提示词的完整哈希即输入公告 ID 与 PDF 内容快照的等价物（文档 ID 与片段内容都写进了提示词）。

缓存值：经过校验的结构化响应、用量、成本、生成时间、解析状态、校验版本。
绝不保存 API Key、Authorization 头或任何密钥。

失败（截断、HTTP 错误、无法解析、结构非法）不会写入缓存。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Optional

from app.core import db


def compute_cache_key(
    config: Any,
    system: str,
    user: str,
    *,
    step: str = "",
    max_tokens: Optional[int] = None,
    prompt_version: str = "1",
    validation_version: str = "1",
) -> str:
    """由不可变调用参数构造缓存键。任何一项变化都会导致缓存失效。"""
    identity = {
        "provider": (getattr(config, "base_url", "") or "").rstrip("/"),
        "model": getattr(config, "model", ""),
        "temperature": getattr(config, "temperature", 0.0),
        "max_tokens": max_tokens or getattr(config, "max_output_tokens", 0),
        "reasoning": getattr(config, "reasoning", "") or "",
        "reasoning_effort": getattr(config, "reasoning_effort", "") or "",
        "step": step,
        "prompt_version": prompt_version,
        "validation_version": validation_version,
        "system_sha256": _sha256(system),
        "user_sha256": _sha256(user),
    }
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    return _sha256(payload)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get(key: str) -> Optional[dict[str, Any]]:
    """读取缓存条目；缺失或损坏返回 None。"""
    return db.get_llm_cache(key)


def put(key: str, *, data: Any, input_tokens: int, output_tokens: int,
        cost_cny: float, validation_version: str, parse_status: str = "ok") -> None:
    """写入缓存条目。由调用方保证仅在成功且结构合法时调用。"""
    db.save_llm_cache(
        key,
        {
            "data": data,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cost_cny": float(cost_cny or 0.0),
            "generated_at": time.time(),
            "parse_status": parse_status,
            "validation_version": validation_version,
        },
    )


# 同键并发去重：每个键一把进程内锁，避免同键并发时重复调用模型。
_key_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def key_lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _key_locks.setdefault(key, threading.Lock())
