"""命令行扫描：等价于 Web 端提交的任务，用于本地验证与批量回归。

用法：
    python scripts/run_scan.py 600519
    python scripts/run_scan.py 00700 --timeout 600
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402
from app.core.models import TaskStatus  # noqa: E402
from app.engine.pipeline import ScanPipeline  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="运行一次基本面排雷扫描")
    parser.add_argument("query", help="股票名称或代码，如 600519 / 00700")
    parser.add_argument("--timeout", type=int, default=0, help="任务超时秒数")
    parser.add_argument("--task-id", default="", help="指定任务 ID")
    args = parser.parse_args()

    db.init_db()
    pipeline = ScanPipeline(
        task_id=args.task_id or None,
        deadline_seconds=args.timeout or None,
    )
    print(f"任务 {pipeline.task_id}：开始扫描 {args.query}")
    started = time.time()
    result = pipeline.run(args.query)
    print(f"\n状态：{result.status.value}  （{result.message}）")
    print(f"耗时：{time.time() - started:.1f} 秒")

    if not result.payload:
        print("未生成报告")
        return 1

    s = result.payload["summary"]
    d = result.payload["data_scope"]
    print(f"\n公司：{result.payload['security']['org_name']} "
          f"({result.payload['security']['secucode']})")
    print(f"最高已确认风险：{s['highest_severity']}")
    print(f"覆盖等级：{result.payload['scan'].get('coverage_level') or '—'}")
    print(f"风险 {s['risk_count']} 项 · 关注 {s['watch_count']} 项 · 数据不足 {s['insufficient_count']} 项")
    print(f"覆盖：{s['coverage']['evaluated']}/{s['coverage']['applicable']} 适用检查项")
    print(f"公告 {d['announcement_fetched']} 条 · 下载 {d['documents_downloaded']} 份 · "
          f"解析 {d['documents_parsed']} 份 · 证据 {d['evidence_count']} 条（复核通过 {d['evidence_verified']}）")
    print(f"\nHTML 报告：{result.html_path}")
    print(f"JSON 数据：{result.json_path}")

    print("\n最重要的发现：")
    for f in s["top_findings"]:
        print(f"  [{f['status']}/{f['severity']}] {f['name']}：{f['finding'][:90]}")
    if not s["top_findings"]:
        print("  （无风险或关注级别的发现）")

    print(f"\n覆盖缺口 {len(result.payload['gaps'])} 项")
    for g in result.payload["gaps"][:8]:
        print(f"  - {g[:120]}")
    return 0 if result.status is not TaskStatus.FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
