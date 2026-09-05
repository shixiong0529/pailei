import json
import sys

tid = sys.argv[1]
d = json.load(open(f"data/reports/{tid}.json"))
s = d["summary"]
sc = d["data_scope"]
print("公司:", d["security"]["org_name"], d["security"]["secucode"])
print("状态:", d["scan"]["status"], "| 耗时", d["scan"]["elapsed_seconds"], "s")
print("风险", s["risk_count"], "| 关注", s["watch_count"],
      "| 数据不足", s["insufficient_count"],
      "| 覆盖", s["coverage"]["evaluated"], "/", s["coverage"]["applicable"])
print("公告", sc["announcement_fetched"], "| 原文", sc["documents_downloaded"],
      "| 解析", sc["documents_parsed"], "| 证据", sc["evidence_count"],
      "| 复核通过", sc["evidence_verified"])
print("AI:", d["ai"]["usage"]["reason"] or "已启用")
print("缺口:", d["gaps"])
print("前3发现:")
for f in s["top_findings"][:3]:
    print("  -", f["status"], f["name"], "|", f["finding"][:70])
