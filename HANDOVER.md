# 交接报告：A 股 / 港股基本面排雷 Agent

> **2026-09-06 V1.2 可信度改造更新**：报告版本升至 V1.2，规则版本保持 1.1（52 项规则与阈值不变）。
> V1.2 新增模型结果缓存与可复现、AI 候选事件分层、事件生命周期与按需历史追溯、任务状态与覆盖等级分离、
> 资料选择与目标章节解析、行业规则能力口径、运行诊断与存储清理。原有 58 项确定性测试 + 89 项验收回归
> 保持通过，新增 V1.2 各阶段测试 103 项，共 **250 项**。完整变更、迁移、样本与回滚见
> [V1.2 实施报告](docs/V1.2_IMPLEMENTATION_REPORT.md)。旧报告须重新扫描才会使用 V1.2 口径。

> **2026-09-06 修复更新**：诊断 A01—A31 的程序修复已实施，规则版本 1.1。原有 58 项 + 验收回归 53 项 + 独立边界测试 36 项通过，共 147 项。完整变更、测试证据及限制见 [修复与回归记录](docs/FIX_REPORT_2026-09-06.md)。下文原有 2026-09-05 结果是历史记录；以本更新和修复记录为准。旧报告须重新扫描才会使用新规则。

> **2026-09-06 性能更新**：模型分批保持原输入与校验，最多 2 路并行；真实 `00700.HK` Web 报告由 120.030 秒降至 61.383 秒。预算并发安全、质量对照和回退配置见 [耗时实测与优化记录](docs/PERFORMANCE_OPTIMIZATION_2026-09-06.md)。

> 交接目的：供后续验收、代码审核和维护使用。本文自包含项目全貌、关键设计决策、
> 可执行的验收步骤与建议的审核重点。初版撰写于 2026-09-05，当前状态更新于 2026-09-06。

---

## 1. 项目定位

输入股票名称或代码 → 识别公司 → 拉取财报与公告原文 → **程序计算指标 + 规则引擎判定**
→ 输出每条结论都绑定原始证据（文件 + 页码 + 指纹 + 复核）的离线 HTML 排雷报告。

**核心产品原则（审核时请验证这些原则没被破坏）：**

1. 模型（LLM）只做解释与核验，**绝不参与数值计算**；未配置密钥时相关步骤整体跳过并标注，绝不伪造
2. 不输出爆雷概率；风险信号评分只是"检查项扣分加权汇总"，报告内附免责行
3. 每条风险结论必须绑定到具体披露文件与页码，生成后程序做原文复核，复核未通过的在报告中标注
4. 数据不足的检查项输出"数据不足，无法判断"，不硬造结论

## 2. 技术架构

```
FastAPI (app/main.py)
  ├── 线程池(5) 后台任务 ──→ ScanPipeline (app/engine/pipeline.py)
  │        身份识别 → 检索规划 → 资料获取 → 标准化 → 规则检查 → 专项阅读 → 核验 → 报告
  ├── SQLite (app/core/db.py, 12 张表：任务/事实/文档/证据/规则结果/事件/报告版本/日志/健康/LLM用量/阶段耗时/运行时统计)
  ├── 数据适配层 (app/data/)：eastmoney 财报、cninfo A股公告、hkexnews 港股公告、
  │        identity 身份识别（含 A/H 关联）、pdftext PDF 解析与证据定位
  ├── 规则引擎 (app/engine/rules/)：通用 36 项 + 行业包 16 项（银行/保险/券商/地产各 4 项）
  ├── LLM 适配层 (app/llm/adapter.py)：OpenAI 兼容协议，当前 GLM-5.3-Flash；含结果缓存（V1.2）
  └── 报告渲染 (app/report/render.py + templates/report.html.j2)：Jinja2 StrictUndefined
```

数据流关键点：**`data/reports/{task_id}.json` 是报告的唯一事实源**，HTML 由它渲染；
SQLite `reports` 表存 payload 供在线页使用；在线页每请求用同一模板实时渲染（在线 = 下载）。

## 3. 关键设计决策（含理由）

| 决策 | 理由 | 位置 |
|---|---|---|
| 指标全部程序计算，LLM 只解释 | 消除模型算错数的风险 | `app/engine/metrics.py` |
| 独立模型批次最多 2 路并行，按输入顺序合并 | 保持材料和校验不变，减少串行等待 | `app/llm/adapter.py` |
| StrictUndefined 模板 | 未定义变量直接报错，不出空白报告 | `app/report/render.py` |
| 证据带内容指纹 + 事后复核 | 防止"引用不存在的原文" | `app/data/pdftext.py` |
| 渲染层过滤（rejectattr）而非删数据 | 02 矩阵隐藏"不适用/数据不足"行，但 JSON 完整保留 52 项结果 | 模板 `sec-02` |
| 评分在渲染层计算 | 历史 JSON 无需迁移即可重渲染出新评分 | `render.py: risk_signal_score()` |
| 分批调用 LLM | 思考模型推理计入 completion_tokens，整包提交会截断 JSON；单批失败只丢该批 | `app/llm/adapter.py: _run_batched` |
| 渲染层 URL 白名单（仅 http/https） | 防 javascript: 等注入 | `render.py: safe_url()` |
| SSRF 防护默认禁内网地址 | 数据源全是公网接口 | `app/core/http_client.py` |
| 每主机 2 rps 限流 | 对公开数据源保持克制 | 同上 |

## 4. 本迭代（2026-09-05 晚）新增/变更清单

1. **LLM 完整接入**：GLM-5.3-Flash（智谱 OpenAI 兼容）；修复思考模型 JSON 截断
   （识别 `finish_reason=length` + `interpret_anomalies`/`extract_events` 分批）；
   实测 6/6 解读生效，单次约 ¥0.05
2. **风险信号评分**：基础 100，发现风险 高-12/中-8/低-4、需要关注 中-2/低-1，下限 0；
   评级 A≥95/B≥80/C≥60/D≥35/E<35；01 节顶部评分卡（分数 + 评级徽章 + 扣分构成 + 免责行）
3. **报告重构**：9 节 → 4 节（风险总览 / 逐项检查结果 / 问题详情与原文 / 数据来源与算法），
   体积 -65%；报告目录 + 锚点跳转；01/03 去重（01 是索引表）；02 长说明折叠（why > 60 字）；
   标题格式"证券简称（代码）排雷报告"
4. **身份识别修复**：`_resolve_exact` 路径 `name=code` → enrich 增加短名回填
   （search 按代码精确匹配取 Name）
5. **测试**：47 → 58 项（TestRiskScore 11 项 + 渲染断言）
6. 移除死代码：`charts.severity_bar`、dim-fold 相关 CSS

## 5. 验收步骤（可执行）

```bash
# 环境：Python 3.12+；依赖：pip install -r requirements.txt
# .env 已配置 GLM-5.3-Flash 密钥（本机）；无 .env 时全部功能仍可运行（AI 步骤跳过）

# ① 原有单元测试（离线）——预期：58 项全部 OK
python tests/run_tests.py

# ② 验收回归与边界测试（离线）——预期：192 项全部 OK
python -m unittest discover -s tests -p 'test_*.py' -v

# ③ 启动服务——预期：Uvicorn running on http://127.0.0.1:8770
python -m uvicorn app.main:app --host 127.0.0.1 --port 8770

# ④ 健康检查——预期：{"ok":true,...}
curl -s http://127.0.0.1:8770/api/health

# ⑤ 提交真实扫描
curl -s -X POST http://127.0.0.1:8770/api/scan \
  -H 'Content-Type: application/json' -d '{"query":"600519"}'
# 然后轮询 GET /api/tasks/{task_id} 直至 status=完成，再打开 /report/{task_id}

# ⑥ 当前规则报告抽查
open http://127.0.0.1:8770/report/66b1fafbfe97    # 腾讯，规则版本 1.1

# ⑦ CLI 方式等价验证
python scripts/run_scan.py 600519

# ⑧ 停止后台服务；前台运行时直接按 Ctrl+C
PID=$(lsof -tiTCP:8770 -sTCP:LISTEN)
if [ -n "$PID" ]; then kill "$PID"; fi
```

**报告验收要点**（对照 4 节结构）：
- 01 有评分卡（分数/评级/扣分构成/免责行），发现清单可点击跳转 03
- 02 无"不适用"与"数据不足"行；公司治理长说明折叠为"展开说明（N 字）"
- 03 原始证据默认折叠，点击展开；打印预览时自动全部展开
- 下载的 HTML 断网可开（0 外部资源）；手机端表格转换为字段卡片，320/390/430px 无整页横向溢出，PC 仍为五列表格

## 6. 建议审核重点

按风险排序，最值得挑刺的地方：

1. **`app/llm/adapter.py`**：`chat_json` 的截断识别与 `_run_batched` 的部分失败合并逻辑
   （预算耗尽时有部分结果是否正确收工、failures 是否如实上报）
2. **`app/report/render.py: risk_signal_score()`**：扣分数学与评级边界（95/80/60/35 恰好在阈值上的归属）
3. **模板安全**：`report.html.j2` 动态文字是否全部经 Jinja2 自动转义（有无 `|safe` 滥用——
   目前仅评分块无、SVG 由受控代码生成）；`safe_url` 白名单是否可绕过
4. **`app/data/identity.py: enrich()`**：短名回填的异常路径（search 失败不阻断）
5. **并发与状态**：线程池 5 并行下 SQLite 写入（connect 上下文管理器是否正确提交/回滚）
6. **规则误报治理**：`DEVELOPMENT_STATUS.md` 第二节的 6 个已修复误报，回归测试是否覆盖

## 7. 已知问题与限制（验收时不应视为缺陷）

- "数据不足"检查项不进入主结论矩阵，在风险总览的折叠清单中完整列出
- 历史报告 JSON 中部分 `security.name` 仍为代码（仅 603986 已补正；新生成的不受影响）
- 历史风险跨年追溯已实现（V1.2 阶段 3，按需触发，受查询/公告/下载/耗时上限约束）
- 数据源为公开网页接口，商用授权未确认；巨潮对高频抓取敏感（已限流 2 rps）
- `data/publish/`（外部分享链接）为手工发布流程，未自动化

## 8. 文件地图

| 文件/目录 | 职责 |
|---|---|
| `app/main.py` | FastAPI 入口：页面路由、API、后台任务调度 |
| `app/config.py` | 全部配置（环境变量 / .env，`.env.example` 是唯一权威清单） |
| `app/core/` | 数据对象（models）、SQLite（db）、安全 HTTP 客户端、简繁文本工具 |
| `app/data/` | eastmoney / cninfo / hkexnews / identity / pdftext 五个适配器 |
| `app/engine/` | normalize（标准化）、metrics（指标）、rules（规则引擎）、gaps（缺口分类）、selection（资料选择）、pipeline（编排） |
| `app/llm/adapter.py` | OpenAI 兼容适配 + 分批调用 + 预算控制 + 结果缓存 |
| `app/report/` | render.py（上下文构建 + 评分）、charts.py（SVG）、templates/（唯一模板） |
| `app/validation/` | V1.2 固定验收集（checklist.json）与报告语义差异工具 |
| `app/core/storage.py` | V1.2 内容寻址存储：SHA256 硬链接去重 |
| `app/web/` | 首页/进度页/历史/管理的页面模板与静态资源 |
| `tests/run_tests.py` | 58 项原有确定性测试（离线） |
| `tests/test_acceptance.py` | 53 项验收回归测试（离线） |
| `tests/test_hardening.py` | 36 项独立边界与集成测试（离线） |
| `tests/test_{validation,llm_cache,events,lifecycle,status,selection,capability,diagnostics}.py` | V1.2 阶段 0—7 测试，共 103 项 |
| `docs/RULES.md` | 52 项规则完整参考（改规则必须同步更新） |
| `docs/V1.2_IMPLEMENTATION_REPORT.md` | V1.2 实施报告：最终行为、迁移、测试、样本、回滚 |
| `DEVELOPMENT_STATUS.md` | 开发状态：已完成、误报修复、未实现清单、续接指引 |
| `scripts/` | run_scan（CLI）、phase0_validate（数据源验证）、inspect_report（报告检查）、cleanup（缓存清理） |

## 9. 验收判定建议

- **通过**：250 项测试全过 + ⑤ 或 ⑥ 至少一条链路跑通 + 报告验收要点全部满足
  + 第 6 节审核重点无 P0 级发现
- **有条件通过**：发现 P1（如某个边界条件错误但主流程正确）→ 列清单修复后复验
- **不通过**：安全类问题（XSS/SSRF 绕过）、伪造证据/结论、模型参与数值计算
