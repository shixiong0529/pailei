"""报告语义差异工具。

用于可重复验收：比较两份报告的结构化事实源（`data/reports/{task_id}.json`），
忽略任务 ID、生成时间、抓取时间等易变字段，重点比较证券身份、财务事实、指标、
规则、证据引文、缺口与事件。

差异分为三类：
- deterministic：程序确定性内容（证券、事实、指标、规则状态、证据、缺口）；
- model：生成式模型内容（ai 段落、模型补充事件）；
- metadata：运行元数据（时间、路径、指纹等易变字段，不影响语义一致性）。
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterator

# 易变/运行元数据字段：递归剥离，单独报告差异。
METADATA_FIELDS = {
    "task_id", "generated_at", "rendered_at", "started_at", "finished_at",
    "created_at", "elapsed_seconds", "elapsed_ms", "fetched_at", "local_path",
    "sha256", "size_bytes", "html_path", "json_path", "updated_at", "parsed_at",
    "page_count", "report_id",
}

# 模型相关顶层段落（由生成式模型产生）。
MODEL_TOP_KEYS = {"ai"}
# 模型补充事件的 event_id 前缀。
MODEL_EVENT_PREFIX = "evt:ai"


def _is_metadata_key(key: str) -> bool:
    return key in METADATA_FIELDS


def _is_model_path(path: str) -> bool:
    if not path:
        return False
    top = path.split(".")[0].split("[")[0]
    return top in MODEL_TOP_KEYS


def _diff_paths(a: Any, b: Any, path: str = "") -> Iterator[tuple[str, str, Any, Any]]:
    """递归比较两份结构，产出 (path, category, a_val, b_val)。"""
    if isinstance(a, dict) and isinstance(b, dict):
        keys = sorted(set(a) | set(b))
        for k in keys:
            p = f"{path}.{k}" if path else k
            if _is_metadata_key(k):
                if (k in a) != (k in b) or (k in a and a[k] != b[k]):
                    yield (p, "metadata", a.get(k), b.get(k))
                continue
            if k not in a:
                yield (p, "deterministic", None, b[k])
            elif k not in b:
                yield (p, "deterministic", a[k], None)
            else:
                yield from _diff_paths(a[k], b[k], p)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            yield (path, "deterministic", _list_summary(a), _list_summary(b))
            return
        for i, (x, y) in enumerate(zip(a, b)):
            yield from _diff_paths(x, y, f"{path}[{i}]")
    else:
        if a != b:
            yield (path, "deterministic", a, b)


def _list_summary(items: list) -> list:
    """列表长度差异时输出可读摘要（首条 + 长度），避免整表倾倒。"""
    return [f"<{len(items)} items>", items[0] if items else None]


def _finalize(
    diffs: list[tuple[str, str, Any, Any]],
    item_mapper: Any = None,
) -> list[dict[str, Any]]:
    out = []
    for path, cat, av, bv in diffs:
        entry = {"path": path, "a": av, "b": bv}
        if item_mapper:
            entry.update(item_mapper(entry))
        out.append(entry)
    return out


def _describe(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": entry["path"],
        "a_short": _short(entry["a"]),
        "b_short": _short(entry["b"]),
    }


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 120:
        return value[:120] + "…"
    if isinstance(value, list) and len(value) > 3:
        return value[:3] + [f"…<{len(value)}>"]
    return value


def compare_reports(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """比较两份报告，返回分类差异与语义一致性判定。

    `semantically_identical` 在确定性内容与模型内容都一致时为 True；
    运行元数据差异不影响语义一致性。
    """
    a = copy.deepcopy(a or {})
    b = copy.deepcopy(b or {})
    raw = list(_diff_paths(a, b))
    deterministic, model, metadata = [], [], []
    for path, cat, av, bv in raw:
        if cat == "metadata":
            metadata.append({"path": path, "a": av, "b": bv})
        elif _is_model_path(path):
            model.append({"path": path, "a": av, "b": bv})
        else:
            deterministic.append({"path": path, "a": av, "b": bv})

    deterministic = [_describe(d) for d in deterministic]
    model = [_describe(d) for d in model]
    metadata = [_describe(d) for d in metadata]

    return {
        "deterministic_identical": not deterministic,
        "model_identical": not model,
        "semantically_identical": not deterministic and not model,
        "deterministic_diffs": deterministic,
        "model_diffs": model,
        "metadata_diffs": metadata,
    }


def semantic_fingerprint(report: dict[str, Any]) -> str:
    """计算确定性语义指纹：剥离易变字段与模型段落后做稳定序列化。

    用于快速判断两份报告的确定性内容是否一致。
    """
    canonical = _canonical_semantic(report)
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_semantic(obj: Any) -> Any:
    """剥离元数据字段与模型段落，返回确定性语义快照。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_metadata_key(k) or k in MODEL_TOP_KEYS:
                continue
            out[k] = _canonical_semantic(v)
        return out
    if isinstance(obj, list):
        return [_canonical_semantic(item) for item in obj]
    return obj


def load_report(path: str) -> dict[str, Any]:
    """从磁盘加载报告 JSON。"""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
