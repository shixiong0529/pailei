"""原始文件按内容指纹去重。

同一内容的 PDF 只保留一份物理副本：下载完成后按 SHA256 归入内容寻址的
`_blob/{sha256}.pdf`，多个公告 ID 指向同一内容时共享同一 inode，避免因重复
扫描或不同公告 ID 保存多份相同文件。使用硬链接而非复制，不额外占用磁盘；
文件系统不支持硬链接时退回复制或原路径，保证不丢数据。

对外只暴露 `dedup(path, sha256)`：把已下载的文件归入 blob 库并返回内容寻址路径。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.config import settings


def blob_path(sha256: str) -> Path:
    return settings.files_dir / "_blob" / f"{sha256}.pdf"


def dedup(path: Path, sha256: str) -> Path:
    """按内容指纹去重后返回内容寻址路径。

    - blob 已存在：把 path 替换为指向 blob 的硬链接（消除重复 inode），返回 blob；
    - blob 不存在：为 path 建立硬链接（保留原 path 作为下载缓存），返回 blob；
    - 任何文件系统错误都退回原 path，不丢数据、不中断扫描。
    """
    if not sha256:
        return path
    path = Path(path)
    blob = blob_path(sha256)
    try:
        if blob.exists():
            try:
                if path.resolve() != blob.resolve():
                    path.unlink(missing_ok=True)
                    os.link(blob, path)
            except OSError:
                pass
            return blob
        blob.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, blob)
        except OSError:
            import shutil
            shutil.copy2(path, blob)
        return blob
    except OSError:
        return path
