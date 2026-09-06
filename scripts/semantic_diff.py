"""报告语义差异 CLI。

用法：
    python scripts/semantic_diff.py <report_a.json> <report_b.json>
    python scripts/semantic_diff.py <report_a.json> <report_b.json> --quiet

输出区分确定性差异、模型差异、运行元数据差异，并给出语义一致性判定。
退出码：语义一致返回 0，存在确定性差异返回 1。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.validation.semantic_diff import compare_reports  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="比较两份排雷报告 JSON 的语义差异")
    parser.add_argument("report_a", help="报告 A 的 JSON 路径")
    parser.add_argument("report_b", help="报告 B 的 JSON 路径")
    parser.add_argument("--quiet", action="store_true", help="只输出判定结论")
    args = parser.parse_args()

    with open(args.report_a, encoding="utf-8") as fh:
        a = json.load(fh)
    with open(args.report_b, encoding="utf-8") as fh:
        b = json.load(fh)

    result = compare_reports(a, b)
    if args.quiet:
        verdict = "语义一致" if result["semantically_identical"] else "存在语义差异"
        print(verdict)
        return 0 if result["semantically_identical"] else 1

    print(f"确定性一致：{'是' if result['deterministic_identical'] else '否'}")
    print(f"模型一致：{'是' if result['model_identical'] else '否'}")
    print(f"语义一致：{'是' if result['semantically_identical'] else '否'}")
    print(f"\n确定性差异 {len(result['deterministic_diffs'])} 处：")
    for d in result["deterministic_diffs"][:50]:
        print(f"  - {d['path']}")
        print(f"      A: {d['a_short']}")
        print(f"      B: {d['b_short']}")
    print(f"\n模型差异 {len(result['model_diffs'])} 处：")
    for d in result["model_diffs"][:20]:
        print(f"  - {d['path']}")
    print(f"\n运行元数据差异 {len(result['metadata_diffs'])} 处（不影响语义一致性）")
    return 0 if result["semantically_identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
