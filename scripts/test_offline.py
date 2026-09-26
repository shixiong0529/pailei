"""运行全部离线测试，隔离默认存储并阻断真实网络及真实模型密钥。

python scripts/test_offline.py
所有 mock 仍可覆盖网络边界以验证重定向、SSRF、限流与模型协议。
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix='pailei-offline-suite-') as directory:
        root = Path(directory)
        # 在第一次 import config 前设置，.env 不覆盖这些值。
        os.environ['DB_PATH'] = str(root / 'app.db')
        os.environ['LLM_API_KEY'] = ''
        os.environ['ENABLE_NETWORK'] = 'true'  # 实际连接由下面的 guard 拦截；供 mock 契约使用
        os.environ['ENABLE_LLM'] = 'true'
        os.environ['LLM_ENABLED'] = 'true'
        from app.config import settings
        from app.core import db
        settings.db_path = root / 'app.db'
        settings.files_dir = root / 'files'
        settings.cache_dir = root / 'cache'
        settings.reports_dir = root / 'reports'
        db.init_db()

        def offline(*args, **kwargs):
            raise OSError('offline test suite: unexpected real network operation')

        with patch.object(socket, 'getaddrinfo', side_effect=offline), \
             patch.object(socket.socket, 'connect', side_effect=offline), \
             patch.object(socket.socket, 'connect_ex', side_effect=offline):
            original = __import__('run_tests')
            loader = unittest.TestLoader()
            suite = unittest.TestSuite([
                loader.loadTestsFromModule(original),
                loader.discover(str(ROOT / 'tests'), pattern='test_*.py'),
            ])
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        # 不留存后台测试任务；夹具不应启动真实扫描。
        from app import main as web
        web.executor.shutdown(wait=True, cancel_futures=True)
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
