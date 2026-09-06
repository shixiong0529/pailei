"""披露文件解析：PDF 文本提取与原文证据定位。

设计约束（方案 §5 / §9）：
- 限制解析页数与超时，避免长文档拖垮任务；
- 证据必须定位到页码，且片段指纹可复核；
- 外部文档内容一律按数据处理，不参与指令执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from app.config import settings
from app.core.models import DisclosureDoc, Evidence, now_iso

MAX_QUOTE = 900


@dataclass
class ParsedDoc:
    doc_id: str
    page_count: int
    pages: list[tuple[int, str]]
    truncated: bool
    error: str = ""

    def text_of(self, page: int) -> str:
        for no, text in self.pages:
            if no == page:
                return text
        return ""

    @property
    def full_text(self) -> str:
        return "\n".join(t for _, t in self.pages)


def _parse_pdf(path: str | Path, max_pages: int | None = None) -> ParsedDoc:
    """提取 PDF 文本。失败时返回带 error 的空结果，不抛异常。"""
    max_pages = max_pages or settings.max_pdf_pages
    path = Path(path)
    doc_id = path.stem
    if not path.exists():
        return ParsedDoc(doc_id, 0, [], False, error="文件不存在")
    try:
        import pdfplumber

        pages: list[tuple[int, str]] = []
        total = 0
        with pdfplumber.open(str(path)) as pdf:
            total = len(pdf.pages)
            for idx, page in enumerate(pdf.pages[:max_pages], start=1):
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                pages.append((idx, _clean(text)))
        return ParsedDoc(doc_id, total, pages, truncated=total > max_pages)
    except Exception as exc:  # 解析失败必须被记录，而不是中断扫描
        return ParsedDoc(doc_id, 0, [], False, error=f"{type(exc).__name__}: {exc}"[:300])


def parse_pdf(path: str | Path, max_pages: int | None = None, *, timeout: float = 60) -> ParsedDoc:
    """单文件解析在可终止子进程中执行，慢页不能占满任务线程。"""
    import json
    import subprocess
    import sys
    if timeout <= 0:
        return ParsedDoc(Path(path).stem, 0, [], True, "解析超时：任务期限已到")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.data.pdftext", str(Path(path).resolve()),
             str(max_pages or settings.max_pdf_pages)],
            cwd=str(Path(__file__).resolve().parents[2]), capture_output=True,
            text=True, timeout=timeout, check=True,
        )
        data = json.loads(result.stdout)
        data["pages"] = [tuple(page) for page in data["pages"]]
        return ParsedDoc(**data)
    except subprocess.TimeoutExpired:
        return ParsedDoc(Path(path).stem, 0, [], True, "PDF 解析超时，已终止解析进程")
    except Exception as exc:
        return ParsedDoc(Path(path).stem, 0, [], False, f"PDF 解析失败：{type(exc).__name__}")


def _clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def locate_quote(
    parsed: ParsedDoc,
    keywords: Sequence[str],
    *,
    window: int = 260,
) -> tuple[int, str] | None:
    """在文档中定位同时命中关键词最多的片段，返回 (页码, 片段)。"""
    best: tuple[int, str] | None = None
    best_score = 0
    for page_no, text in parsed.pages:
        if not text:
            continue
        lowered = text.lower()
        hits = sum(1 for k in keywords if k and k.lower() in lowered)
        if hits == 0:
            continue
        idx = -1
        for k in keywords:
            if not k:
                continue
            pos = lowered.find(k.lower())
            if pos >= 0:
                idx = pos
                break
        start = max(0, (idx if idx >= 0 else 0) - window // 2)
        snippet = text[start : start + MAX_QUOTE]
        if hits > best_score:
            best_score = hits
            best = (page_no, snippet)
    return best


def build_evidence(
    doc: DisclosureDoc,
    parsed: ParsedDoc,
    keywords: Sequence[str],
    *,
    fallback_quote: str = "",
) -> Evidence | None:
    """构造可核验的证据。若原文内找不到命中，则不使用该文档作为证据。"""
    found = locate_quote(parsed, keywords)
    if not found:
        if not fallback_quote:
            return None
        return Evidence(
            evidence_id=f"{doc.doc_id}:meta",
            doc_id=doc.doc_id,
            title=doc.title,
            quote=fallback_quote[:MAX_QUOTE],
            location="文件元数据",
            url=doc.url,
            source=doc.source,
            publish_date=doc.publish_date,
            fingerprint=Evidence.fingerprint_of(fallback_quote[:MAX_QUOTE]),
            verified=False,
            verify_note="来自公告标题/元数据，未定位到正文片段",
        )
    page_no, snippet = found
    return Evidence(
        evidence_id=f"{doc.doc_id}:p{page_no}:{Evidence.fingerprint_of(snippet)}",
        doc_id=doc.doc_id,
        title=doc.title,
        quote=snippet[:MAX_QUOTE],
        location=f"第 {page_no} 页" + (f"（共 {parsed.page_count} 页）" if parsed.page_count else ""),
        url=doc.url,
        source=doc.source,
        publish_date=doc.publish_date,
        fingerprint=Evidence.fingerprint_of(snippet),
        verified=True,
        verify_note="已在原文中定位并复核",
    )


def verify_evidence(evidence: Evidence, parsed: ParsedDoc) -> Evidence:
    """独立核验：确认片段确实出现在所声称的文档页面中。"""
    evidence.verified = False
    evidence.verify_note = "核验未通过：缺少有效页码或正文"
    if parsed.doc_id != evidence.doc_id:
        evidence.verify_note = "核验未通过：文档身份不一致"
        return evidence
    if not evidence.location.startswith("第"):
        return evidence
    match = re.search(r"第\s*(\d+)\s*页", evidence.location)
    if not match:
        return evidence
    page_no = int(match.group(1))
    page_text = parsed.text_of(page_no)
    if not page_text:
        evidence.verified = False
        evidence.verify_note = "核验失败：该页无文本内容"
        return evidence
    fingerprint = Evidence.fingerprint_of(evidence.quote)
    if evidence.fingerprint and evidence.fingerprint != fingerprint:
        evidence.verify_note = "核验未通过：片段指纹不一致"
        return evidence
    evidence.fingerprint = fingerprint
    key = re.sub(r"\s+", "", evidence.quote)
    if key and re.sub(r"\s+", "", page_text).find(key) >= 0:
        evidence.verified = True
        evidence.verify_note = "已复核：片段存在于所标页码"
    else:
        evidence.verified = False
        evidence.verify_note = "核验未通过：片段未出现在所标页码"
    return evidence


def search_pages(parsed: ParsedDoc, pattern: str, limit: int = 5) -> list[tuple[int, str]]:
    """按正则搜索正文，用于规则定位特定附注。"""
    try:
        regex = re.compile(pattern)
    except re.error:
        return []
    out: list[tuple[int, str]] = []
    for page_no, text in parsed.pages:
        if regex.search(text):
            out.append((page_no, text[:MAX_QUOTE]))
            if len(out) >= limit:
                break
    return out


def find_quote_page(parsed: ParsedDoc, quote: str) -> int | None:
    """定位完整引文所在的页码（忽略空白差异）。

    要求引文完整出现在同一页内，防止“前缀命中 + 尾部伪造”被接受。
    找不到或引文为空返回 None。
    """
    key = re.sub(r"\s+", "", quote or "")
    if not key:
        return None
    for page_no, text in parsed.pages:
        if not text:
            continue
        if key in re.sub(r"\s+", "", text):
            return page_no
    return None


def summarize_parsed(parsed: ParsedDoc) -> dict[str, object]:
    return {
        "doc_id": parsed.doc_id,
        "page_count": parsed.page_count,
        "parsed_pages": len(parsed.pages),
        "chars": sum(len(t) for _, t in parsed.pages),
        "truncated": parsed.truncated,
        "error": parsed.error,
        "parsed_at": now_iso(),
    }


# ------------------------------------------------------------------ 审计意见识别

# 模板化 / 否定式表述：出现这些上下文时，关键词不代表非标意见
_BOILERPLATE_MARKERS = (
    "非标准审计意见涉及事项",   # 半年报中的固定勾选项
    "□适用",                    # 未勾选的复选框
    "√不适用",
    # 标准无保留审计报告中的免责句。注意：真正的持续经营不确定性段落
    # 用的是「存在可能导致……持续经营能力……重大疑虑」，不含“未来的事项或情况”，
    # 因此这里用完整句式而非单独的“可能导致”来排除，避免误杀真实信号。
    "未来的事项或情况",
    "不对其发表意见",
)

# 肯定式非标意见：只接受“出具/发表了……保留意见”这类断言
_AFFIRMATIVE_OPINION_PATTERNS = [
    (r"(?:出具|发表|发表了|出具了|形成|形成了)[^。；\n]{0,20}?(保留意见|否定意见|无法表示意见)",
     "非标准审计意见", "high"),
    (r"(?:审计意见类型(?:为|是)|意见类型(?:为|是))[^。；\n]{0,10}?(保留意见|否定意见|无法表示意见)",
     "非标准审计意见", "high"),
    (r"持续经营[^。；\n]{0,12}重大不确定性", "持续经营重大不确定性", "medium"),
    (r"持续经营能力[^。；\n]{0,15}(?:产生|存在)[^。；\n]{0,10}重大疑虑",
     "持续经营重大不确定性", "medium"),
    # 无法表示意见 / 否定意见：属于特定意见类型，几乎不可能出现在模板化表述中
    (r"无法表示意见", "无法表示意见", "high"),
    (r"否定意见", "否定意见", "high"),
    (r"保留意见", "保留意见", "high"),
]

_COMPILED_OPINION_PATTERNS = [
    (re.compile(p), label, sev) for p, label, sev in _AFFIRMATIVE_OPINION_PATTERNS
]


def _is_boilerplate(context: str) -> bool:
    return any(marker in context for marker in _BOILERPLATE_MARKERS)


# 否定词：出现在关键词之前时，表示“没有”非标意见
_NEGATION_RE = re.compile(r"(未|无|没有|不存在|并非|不是|未出现|未发现|免于)[^。；\n]{0,12}$")


def _has_negation(prefix: str) -> bool:
    return bool(_NEGATION_RE.search(prefix[-40:]))


def scan_audit_opinions(parsed: ParsedDoc, *, window: int = 160) -> list[dict[str, object]]:
    """否定式敏感的审计意见识别。

    直接在全文搜索“保留意见”“持续经营”会产生大量误报，典型场景：
    - 半年报固定勾选项「上年年度报告非标准审计意见涉及事项 □适用 √不适用」；
    - 标准无保留审计报告中的免责句「未来的事项或情况可能导致……不能持续经营」。

    因此这里只匹配断言式表述，并对命中位置的上下文做模板化排除。
    """
    hits: list[dict[str, object]] = []
    seen: set[str] = set()
    accepted_spans: list[tuple[int, int, int]] = []
    for page_no, original_text in parsed.pages:
        text = audit_simplified(original_text)
        if not text:
            continue
        for pattern, label, severity in _COMPILED_OPINION_PATTERNS:
            for match in pattern.finditer(text):
                span = (page_no, match.start(), match.end())
                # 同一处文本被多条模式命中时只保留一条，避免重复计数
                if any(
                    s[0] == page_no and match.start() < s[2] and s[1] < match.end()
                    for s in accepted_spans
                ):
                    continue
                start = max(0, match.start() - window)
                end = min(len(text), match.end() + window)
                context = text[start:end]
                if "无保留意见" in match.group(0):
                    continue
                if _is_boilerplate(context):
                    continue
                if _has_negation(text[max(0, match.start() - 40) : match.start()]):
                    continue
                key = f"{label}|{re.sub(r'[^一-鿿A-Za-z]', '', context)[:28]}"
                if key in seen:
                    continue
                seen.add(key)
                accepted_spans.append(span)
                hits.append(
                    {
                        "label": label,
                        "severity": severity,
                        "page": page_no,
                        "quote": original_text[start:end].strip()[:MAX_QUOTE],
                        "matched": match.group(0)[:60],
                    }
                )
    return hits


def audit_simplified(text: str) -> str:
    # 一对一字形转换，位置不变，返回的证据仍取原文。
    source = "審計見無發具標準財報於為們續營確類聲險慮來況對適涉及事項並現"
    target = "审计见无发具标准财报于为们续营确类声险虑来况对适涉及事项并现"
    return text.translate(str.maketrans(source, target))


def has_audit_opinion_section(parsed: ParsedDoc) -> bool:
    text = audit_simplified(parsed.full_text)
    return bool(re.search(r"(?:标准无保留意见|无保留意见|我们认为[\s\S]{0,120}公允反映)", text))


if __name__ == "__main__":
    import json
    import sys
    from dataclasses import asdict
    print(json.dumps(asdict(_parse_pdf(sys.argv[1], int(sys.argv[2]))), ensure_ascii=False))
