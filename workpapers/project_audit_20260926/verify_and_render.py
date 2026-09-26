"""来源版本 → 不可覆盖的测试日志 → 审核底稿 → 项目审查报告。"""
from datetime import datetime
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / 'data/raw/project_audit_20260926'
WP = ROOT / 'workpapers/project_audit_20260926'
SOURCE = ROOT / 'data/source_manifest/project_audit_20260926_v1.json'
LOG = RAW / 'final_v1.log'
PATCH = RAW / 'source_changes_v1.diff'

def write_once(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        stream.write(content)

def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True)

tracked = git('ls-files', 'app', 'scripts', 'tests', 'README.md', 'docs/RULES.md').splitlines()
extra = ['scripts/test_offline.py', 'tests/test_project_audit.py']
files = sorted(set(tracked + extra))
hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}
generated_at = datetime.now().astimezone().isoformat(timespec='seconds')
manifest = {'extracted_at': generated_at, 'period': '2026-09-26 code audit; synthetic financial years 2023–2025',
            'git_head': git('rev-parse', 'HEAD').strip(), 'rule_report_version': '1.3.1',
            'query': 'git ls-files app scripts tests README.md docs/RULES.md plus two new test entrypoints',
            'source_hashes': hashes, 'raw_log': str(LOG.relative_to(ROOT)),
            'synthetic_manifest': 'docs/project-audit-20260926/runtime/web/source_manifest.json',
            'research': 'data/raw/project_audit_20260926/research_response.json',
            'secrets': 'No .env, credentials or access tokens collected.'}
write_once(SOURCE, json.dumps(manifest, ensure_ascii=False, indent=2))
write_once(PATCH, git('diff', '--', 'app', 'scripts', 'tests', 'README.md', 'docs/RULES.md'))
with LOG.open('x') as stream:
    run = subprocess.run([sys.executable, 'scripts/test_offline.py'], cwd=ROOT,
                         stdout=stream, stderr=subprocess.STDOUT)
log = LOG.read_text()
count = re.search(r'Ran (\d+) tests', log)
compiled = []
for name in files:
    if name.endswith('.py'):
        compile((ROOT / name).read_text(), name, 'exec')
        compiled.append(name)
diff_check = subprocess.run(['git', 'diff', '--check'], cwd=ROOT, capture_output=True, text=True)
findings = json.loads((WP / 'findings.json').read_text())
all_tests = set()
for path in (ROOT / 'tests').glob('test_*.py'):
    all_tests.update(node.name for node in ast.walk(ast.parse(path.read_text()))
                     if isinstance(node, ast.FunctionDef) and node.name.startswith('test_'))
referenced_tests = {test for bug in findings['bugs'] for test in bug['tests']}
web = json.loads((WP / 'web_checks_v2.json').read_text())
checks = {'test_exit_zero': run.returncode == 0, '387_tests': count is not None and int(count[1]) == 387,
          'test_success': bool(re.search(r'^OK$', log, re.M)),
          'python_compile': True, 'diff_check': diff_check.returncode == 0,
          'all_finding_tests_exist': referenced_tests <= all_tests, 'web_acceptance': web['passed'],
          'source_unchanged_during_verification': all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha for name, sha in hashes.items())}
audit = {'generated_at': generated_at, 'checks': checks, 'passed': all(checks.values()),
         'test_count': int(count[1]) if count else 0, 'compiled_files': len(compiled),
         'defect_groups': len(findings['bugs']), 'raw_log': str(LOG.relative_to(ROOT)),
         'source_manifest': str(SOURCE.relative_to(ROOT)), 'web_checks': 'web_checks_v2.json',
         'exceptions': [findings['incident']], 'raw_versions': '保留所有 before/regression/isolated 与 Web v1/v2 记录，不覆盖旧 Raw。'}
write_once(WP / 'audit_checks_v1.json', json.dumps(audit, ensure_ascii=False, indent=2))
assert audit['passed'], audit

lines = ['# 项目审查与升级决策清单', '',
         f'生成时间：{generated_at}。报告/规则版本：1.3.1。保留 60 项规则。', '',
         f'已阅读 README 并审查数据获取、财务标准化、指标/规则、事件、AI、任务/存储和 Web 报告链路。发现并修复 {len(findings["bugs"])} 组缺陷；新增 45 项回归，全部 387 项测试通过。', '',
         '## 修复清单', '', '| 编号 | 缺陷与影响 | 修复 | 代码 |', '|---|---|---|---|']
for bug in findings['bugs']:
    lines.append(f'| {bug["id"]} | {bug["title"]}：{bug["impact"]} | {bug["fix"]} | {bug["files"]} |')
lines += ['', '每组回归用例名称见 `workpapers/project_audit_20260926/findings.json`。B24 的复用夹具用例位于 `tests/test_hardening.py`，其余新增用例位于 `tests/test_project_audit.py`。', '',
          '## 等你决定的升级', '', '以下均未实施。建议先做 U1/U2/U3；金融股为主要使用场景时提前 U4。工作量是相对估计，不是工期承诺。', '',
          '| 编号 | 优先级 | 功能 | 当前缺口 | 价值 | 工作量 |', '|---|---|---|---|---|---|']
for item in findings['recommendations']:
    lines.append(f'| {item["id"]} | {item["priority"]} | {item["name"]} | {item["gap"]} | {item["reason"]} | {item["effort"]} |')
for item in findings['recommendations']:
    lines += ['', f'### {item["id"]}：{item["name"]}', '', item['proposal'], '', '验收：' + item['acceptance']]
lines += ['', '供应商融资对流动性透明度的影响可参考 [IFRS 官方说明](https://www.ifrs.org/news-and-events/news/2023/05/iasb-increases-transparency-of-companies-supplier-finance/)。附注、减值与财务控制核验方向可参考 [港交所年报审阅公告](https://www.hkex.com.hk/News/Regulatory-Announcements/2024/240126news?sc_lang=en)。以上升级选择和优先级为本次项目审查判断。', '',
          '## 验证与追溯', '',
          '- 来源：`data/source_manifest/project_audit_20260926_v1.json`：Git 基线、文件哈希、提取时点、期间、参数。',
          '- Raw：`data/raw/project_audit_20260926/`：修复前失败、回归日志、最终测试、变更补丁、Web 原始响应及研究响应；合成财务逐笔数据/公告见 `docs/project-audit-20260926/runtime/web/raw/`。',
          '- 底稿：`workpapers/project_audit_20260926/`：缺陷与建议映射、审核检查、Web 验证、测试隔离修复脚本及备份引用。',
          '- 最终结果：本文件由 `verify_and_render.py` 在审核全部通过后生成，未添加底稿外人工调整。',
          '- 检查：387 项离线测试通过、Python 编译检查通过、`git diff --check` 通过、全部缺陷用例引用存在、验证期间源码哈希一致。',
          '- Web：真实启动隔离 FastAPI 服务，经首页提交、八阶段扫描、覆盖提示、报告与证据页目视检查；10 项 HTTP 审核通过，包括下载与在线内容逐字一致、历史记录、诊断、404 与禁用模型。',
          '- 复现：`.venv/bin/python scripts/test_offline.py`；底稿审核脚本使用不可覆盖的版本文件，重新运行需指定新版本路径。', '',
          '## 例外和边界', '', findings['incident'], '']
lines += ['- ' + text for text in findings['limitations']]
write_once(ROOT / 'outputs/PROJECT_AUDIT_2026-09-26.md', '\n'.join(lines) + '\n')
print(json.dumps(audit, ensure_ascii=False, indent=2))
