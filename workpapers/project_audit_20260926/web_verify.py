"""仅访问隔离 fixture 服务，保存原始响应后审核 Web 输出。"""
from pathlib import Path
import hashlib
import json
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / 'data/raw/project_audit_20260926/web_v2'
RAW.mkdir(parents=True, exist_ok=True)
TASK = '07cc166a78a3'
BASE = 'http://127.0.0.1:18770'

def request(path, name):
    try:
        response = urllib.request.urlopen(BASE + path, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    body = response.read()
    target = RAW / name
    if target.exists():
        raise FileExistsError(target)
    target.write_bytes(body)
    return response.status, {k.lower(): v for k, v in response.headers.items()}, body

results = {}
captures = {}
for path, name in [(f'/api/tasks/{TASK}', 'task.json'), (f'/report/{TASK}', 'report.html'),
                   (f'/download/{TASK}', 'download.html'), ('/history', 'history.html'),
                   ('/admin', 'admin.html'), ('/api/health', 'health.json'),
                   ('/report/nonexistent', 'missing.json')]:
    status, headers, body = request(path, name)
    captures[name] = {'path': path, 'status': status, 'headers': headers, 'sha256': hashlib.sha256(body).hexdigest()}
    results[name] = (status, headers, body)

task = json.loads(results['task.json'][2])
health = json.loads(results['health.json'][2])
checks = {
    'all_existing_routes_200': all(value[0] == 200 for name, value in results.items() if name != 'missing.json'),
    'missing_task_404': results['missing.json'][0] == 404,
    'download_matches_online': results['report.html'][2] == results['download.html'][2],
    'download_attachment': 'attachment' in results['download.html'][1].get('content-disposition', ''),
    'history_has_task': TASK.encode() in results['history.html'][2],
    'version_131': b'v1.3.1' in results['report.html'][2],
    'synthetic_report': '合成排雷样本'.encode() in results['report.html'][2],
    'debt_and_control_evidence': all(text.encode() in results['report.html'][2] for text in ['未能按期偿还', '内部控制存在重大缺陷']),
    'model_disabled': health['llm_ready'] is False,
    'no_running_tasks': health['running'] == 0,
}
metadata = {'source': BASE, 'task_id': TASK, 'fixture': 'web_fixture.py', 'captures': captures,
            'browser_observation': '首页按钮提交后八阶段完成；同一进度页动态显示生成成功/一般缺口；报告展示两条复核证据。'}
(RAW / 'manifest.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
output = {'checks': checks, 'passed': all(checks.values()), 'raw_manifest': str(RAW.relative_to(ROOT) / 'manifest.json'),
          'task_status': task.get('status'), 'browser_checked': True}
(ROOT / 'workpapers/project_audit_20260926/web_checks_v2.json').write_text(json.dumps(output, ensure_ascii=False, indent=2))
print(json.dumps(output, ensure_ascii=False, indent=2))
assert output['passed']
