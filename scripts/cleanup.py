"""安全清理命令：默认预览将删除的内容，加 --execute 才实际删除。

仅清理「可再生的衍生缓存」与「被取代的旧报告版本」：
1. PDF 解析缓存（cache_dir/pdf_parse/*.json，按需再生）；
2. 模型结果缓存（llm_cache 表，重新调用模型即可再生）；
3. 旧报告版本（reports 表中同一任务被更新的历史版本行）。

不删除用户报告文件，不删除原始 PDF（files_dir 下的唯一原文）。

用法：
    python scripts/cleanup.py            # 预览将删除的内容
    python scripts/cleanup.py --execute  # 实际删除
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.core import db  # noqa: E402


def collect_pdf_cache() -> list[Path]:
    d = settings.cache_dir / "pdf_parse"
    if not d.exists():
        return []
    return sorted(d.glob("*.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description="清理衍生缓存与旧报告版本")
    parser.add_argument("--execute", action="store_true", help="实际删除；缺省仅预览")
    args = parser.parse_args()

    db.init_db()
    pdf_cache = collect_pdf_cache()
    llm_cache_n = db.llm_cache_count()
    old_reports = db.superseded_report_rows()

    pdf_bytes = sum(p.stat().st_size for p in pdf_cache)
    print("== 清理预览 ==" if not args.execute else "== 执行清理 ==")
    print(f"1) PDF 解析缓存文件：{len(pdf_cache)} 个（约 {pdf_bytes/1024:.0f} KB）")
    print(f"2) 模型结果缓存条目：{llm_cache_n} 条")
    print(f"3) 旧报告版本（历史版本行，不删除报告文件）：{len(old_reports)} 行")

    total = len(pdf_cache) + llm_cache_n + len(old_reports)
    if total == 0:
        print("没有可清理的内容。")
        return 0

    if not args.execute:
        print("\n以上为预览。加 --execute 才会实际删除。")
        print("不会删除：用户报告文件、原始 PDF（files_dir/_blob）。")
        return 0

    removed_files = 0
    for p in pdf_cache:
        try:
            p.unlink(missing_ok=True)
            removed_files += 1
        except OSError as exc:
            print(f"  跳过 {p.name}：{exc}")
    removed_llm = db.clear_llm_cache()
    removed_reports = db.clear_superseded_reports()

    print("\n清理完成：")
    print(f"  - 删除 PDF 解析缓存 {removed_files} 个文件")
    print(f"  - 删除模型结果缓存 {removed_llm} 条")
    print(f"  - 删除旧报告版本 {removed_reports} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
