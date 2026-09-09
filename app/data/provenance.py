"""Immutable public financial response snapshots and reproducible request manifests.

Only the public Eastmoney endpoint calls this helper: never pass model requests or credentials.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from app.config import settings
from app.core.models import now_iso


def _write_once(path: Path, body: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise OSError("原始数据指纹文件内容不一致，拒绝覆盖")
        return
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp_name = stream.name
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError:
            if path.read_bytes() != body:
                raise OSError("原始数据指纹文件内容不一致，拒绝覆盖")
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def preserve_response(response, *, url, params):
    body = json.dumps(response, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
    digest = hashlib.sha256(body).hexdigest()
    root = settings.db_path.parent
    relative = f"raw/eastmoney/{digest}.json"
    _write_once(root / relative, body)
    manifest = {"source": "eastmoney", "url": url, "params": params,
                "fetched_at": now_iso(), "raw_ref": relative, "sha256": digest,
                "format": "decoded JSON response before mapping", "mapping_version": "1.3"}
    record = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
    _write_once(root / 'source_manifest' / (hashlib.sha256(record).hexdigest() + '.json'), record)
    return relative
