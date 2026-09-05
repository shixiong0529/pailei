"""模型接入适配层。

设计约束（方案 §4 / §7 / §9）：
- 模型供应商可替换：统一走 OpenAI 兼容协议，不绑定任何一家；
- 未配置凭证时返回明确不可用状态，绝不用模拟结论冒充真实分析；
- 模型只负责理解与解释，不产出指标数值；
- 外部文档内容一律作为数据处理，模型输出不得改变程序行为；
- 记录 token 与成本，超过预算即停止调用。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.config import LLMConfig, settings
from app.core.db import save_llm_usage


@dataclass
class LLMResult:
    ok: bool
    data: Any = None
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cny: float = 0.0
    skipped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "skipped_reason": self.skipped_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_cny": round(self.cost_cny, 4),
        }


class BudgetExceeded(Exception):
    pass


class LLMAdapter:
    """OpenAI 兼容协议的模型适配器。"""

    def __init__(self, config: LLMConfig | None = None, task_id: str = ""):
        self.config = config or settings.llm
        self.task_id = task_id
        self.spent_cny = 0.0
        self.calls = 0
        self.failures: list[str] = []

    # ------------------------------------------------------------ 状态

    @property
    def available(self) -> bool:
        return bool(settings.enable_llm and self.config.configured)

    @property
    def unavailable_reason(self) -> str:
        if not settings.enable_llm:
            return "已在配置中关闭模型调用（ENABLE_LLM=false）"
        if not self.config.configured:
            return "未配置 LLM_API_KEY，AI 解读与核验步骤不参与本次扫描"
        return ""

    def _check_budget(self, estimated: float) -> None:
        if self.config.budget_cny <= 0:
            return
        if self.spent_cny + estimated > self.config.budget_cny:
            raise BudgetExceeded(
                f"已达到本次扫描的模型预算上限 {self.config.budget_cny:.2f} 元"
            )

    # ------------------------------------------------------------ 调用

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        step: str = "",
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        if not self.available:
            return LLMResult(ok=False, skipped_reason=self.unavailable_reason)

        url = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user[: self.config.max_input_chars]},
            ],
            "temperature": self.config.temperature,
            "max_tokens": max_tokens or self.config.max_output_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self.config.timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"[:200]
            self.failures.append(msg)
            return LLMResult(ok=False, error=f"模型请求失败 {msg}")

        if resp.status_code >= 400:
            # 不回显响应体，避免泄露任何凭证相关信息
            msg = f"HTTP {resp.status_code}"
            self.failures.append(msg)
            return LLMResult(ok=False, error=f"模型返回错误 {msg}")

        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
            finish_reason = body["choices"][0].get("finish_reason") or ""
            usage = body.get("usage") or {}
        except Exception as exc:
            return LLMResult(ok=False, error=f"模型响应解析失败：{type(exc).__name__}")

        tin = int(usage.get("prompt_tokens") or 0)
        tout = int(usage.get("completion_tokens") or 0)
        cost = (
            tin / 1_000_000 * self.config.price_in_cny_per_1m
            + tout / 1_000_000 * self.config.price_out_cny_per_1m
        )
        self.spent_cny += cost
        self.calls += 1
        if self.task_id:
            save_llm_usage(self.task_id, step or "chat", self.config.model, tin, tout, cost)

        # 思考模型（如 glm-5.3-flash）的推理过程计入输出 token，
        # 超出 max_tokens 会把 JSON 拦腰截断，必须在解析前识别
        if finish_reason == "length":
            return LLMResult(
                ok=False,
                error="模型输出被 max_tokens 截断（思考模型推理占用输出预算），该次结果丢弃",
                input_tokens=tin, output_tokens=tout, cost_cny=cost,
            )

        parsed, err = _extract_json(content)
        if err:
            return LLMResult(ok=False, error=err, input_tokens=tin, output_tokens=tout, cost_cny=cost)
        return LLMResult(ok=True, data=parsed, input_tokens=tin, output_tokens=tout, cost_cny=cost)

    # ---------------------------------------------------------- 业务步骤

    def _run_batched(
        self,
        batches: list[Any],
        build_prompt: Any,
        step: str,
    ) -> LLMResult:
        """分批调用并合并结果。

        思考模型的推理过程计入输出 token，一次处理全部条目时 JSON 极易被
        max_tokens 截断；分批后单次输出有界。单批解析失败只丢弃该批并记录，
        不影响其他批次；预算耗尽或整体不可用时立即终止。
        """
        merged: list[Any] = []
        total_in = total_out = 0
        total_cost = 0.0
        failures: list[str] = []
        for batch in batches:
            if not batch:
                continue
            try:
                self._check_budget(0.3)
            except BudgetExceeded as exc:
                if merged:
                    break  # 已有部分结果，预算用尽即收工
                return LLMResult(ok=False, error=str(exc), skipped_reason=str(exc))
            system, user = build_prompt(batch)
            r = self.chat_json(system, user, step=step)
            total_in += r.input_tokens
            total_out += r.output_tokens
            total_cost += r.cost_cny
            if r.skipped_reason and not r.error:
                return r  # 整体不可用（未配置 / 被关闭）
            if r.ok and isinstance(r.data, list):
                merged.extend(r.data)
            elif r.error:
                failures.append(f"批次 {len(batch)} 条：{r.error[:120]}")
        if failures:
            self.failures.extend(failures)
        if not merged:
            return LLMResult(
                ok=False,
                error="；".join(failures)[:300] or "模型未返回任何可解析结果",
                input_tokens=total_in, output_tokens=total_out, cost_cny=total_cost,
            )
        return LLMResult(ok=True, data=merged, input_tokens=total_in, output_tokens=total_out, cost_cny=total_cost)

    def interpret_anomalies(self, context: dict[str, Any]) -> LLMResult:
        """对程序判定为风险/关注的项做解释与缓解因素识别。"""
        if not self.available:
            return LLMResult(ok=False, skipped_reason=self.unavailable_reason)

        def build(batch: list[Any]) -> tuple[str, str]:
            system = (
                "你是财务分析助手。只能基于给定的结构化事实与原文片段进行解释，"
                "不得引入片段之外的数字，不得给出投资建议，不得输出爆雷概率或安全评分。"
                "输出必须是严格的 JSON 数组。"
            )
            user = (
                "以下是程序计算出的候选异常（数值由程序算出，请勿修改）：\n"
                + json.dumps(batch, ensure_ascii=False)[:18000]
                + "\n\n请为每一项输出 JSON 对象，字段：\n"
                "rule_id, explanation(中文，解释为什么值得关注，80-200字), "
                "still_effective(true/false/null 表示根据资料判断该事项当前是否仍存在), "
                "mitigations(字符串数组，缓解因素), to_verify(字符串数组，还需核实什么), "
                "evidence_quote(若片段中出现可引用的关键原文，摘录不超过80字；否则空字符串)。\n"
                "只输出 JSON 数组，不要任何额外文字。"
            )
            return system, user

        items = context.get("items", [])
        batches = [items[i : i + 3] for i in range(0, len(items), 3)]
        return self._run_batched(batches, build, "interpret")

    def extract_events(self, docs_context: list[dict[str, Any]]) -> LLMResult:
        """从公告标题与片段中提取风险事件及后续进展线索。"""
        if not self.available:
            return LLMResult(ok=False, skipped_reason=self.unavailable_reason)

        def build(batch: list[Any]) -> tuple[str, str]:
            system = (
                "你是信息披露分析助手。只从给定公告标题与片段中提取事件，"
                "不得推断片段以外的信息。输出必须是严格的 JSON 数组。"
            )
            user = (
                "以下是该公司近期公告的标题与正文片段：\n"
                + json.dumps(batch, ensure_ascii=False)[:18000]
                + "\n\n请提取其中可能构成基本面风险的事件，每项输出：\n"
                "title(事件标题), occurred_date(YYYY-MM-DD，取自公告日期), "
                "category(财务/偿债/治理/监管/经营 之一), summary(中文，60-150字), "
                "doc_id(对应的公告 ID), resolved(true/false/null), "
                "resolution_note(若已解除则说明依据)。\n"
                "只输出 JSON 数组，不要额外文字。"
            )
            return system, user

        batches = [docs_context[i : i + 10] for i in range(0, len(docs_context), 10)]
        return self._run_batched(batches, build, "events")

    def verify(self, verification_context: dict[str, Any]) -> LLMResult:
        """独立核验：检查结论与证据在主体、时间、语义上是否对应。"""
        if not self.available:
            return LLMResult(ok=False, skipped_reason=self.unavailable_reason)
        if not self.config.verify_enabled:
            return LLMResult(ok=False, skipped_reason="已在配置中关闭独立核验步骤")
        system = (
            "你是审计式核验助手。逐条检查结论是否为其所引用证据所支持，"
            "重点核对：主体是否同一家公司、时间是否对应、语义是否被夸大。"
            "输出必须是严格的 JSON 数组。"
        )
        user = (
            "待核验的结论与证据：\n"
            + json.dumps(verification_context, ensure_ascii=False)[:18000]
            + "\n\n每项输出：rule_id, verdict(一致/夸大/主体不符/时间不符/证据不足), "
            "reason(中文，40-120字), suggested_status(保持/降级为需要关注/降级为数据不足)。\n"
            "只输出 JSON 数组。"
        )
        try:
            self._check_budget(0.3)
            return self.chat_json(system, user, step="verify")
        except BudgetExceeded as exc:
            return LLMResult(ok=False, error=str(exc), skipped_reason=str(exc))

    def usage_summary(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": "" if self.available else self.unavailable_reason,
            "calls": self.calls,
            "spent_cny": round(self.spent_cny, 4),
            "failures": self.failures[:5],
        }


def _extract_json(content: str) -> tuple[Any, str]:
    """从模型输出中提取 JSON。模型常带代码块包裹，需要稳健解析。"""
    text = (content or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    candidates = [text]
    array_match = re.search(r"\[.*\]", text, re.S)
    if array_match:
        candidates.append(array_match.group(0))
    obj_match = re.search(r"\{.*\}", text, re.S)
    if obj_match:
        candidates.append(obj_match.group(0))
    for candidate in candidates:
        try:
            return json.loads(candidate), ""
        except json.JSONDecodeError:
            continue
    return None, "模型输出不是可解析的 JSON，已丢弃该次结果"
