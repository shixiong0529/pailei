# A 股 / 港股基本面排雷 Agent 全面验收与诊断报告

> **后续状态（2026-09-06）**：本报告是修复前的诊断快照。A01—A31 已逐项修复，原有 58 项、验收回归 53 项和独立边界测试 36 项均已通过，当前规则版本为 1.1。最终实现和复验证据见 [项目程序修复与回归记录](FIX_REPORT_2026-09-06.md)。下文的“不通过”结论只适用于本报告记录的修复前代码。

验收日期：2026-09-06（Asia/Shanghai）
验收对象：本地工作区 `/Users/shixiong/Developer/pailei`；Git HEAD `5be1083f20e8d113b6d034a72bc11bbb158fefaf`
验收基线：开发方案、HANDOVER.md、DEVELOPMENT_STATUS.md、README.md、docs/RULES.md，以及当前实际代码和报告。
本轮范围：只检查、诊断、测试和出报告；未修改业务代码、未增删产品功能、未修改原有 `.env`、数据库或历史报告。新增内容限本报告及 `docs/acceptance-2026-09-06/` 中的验收材料。

## 1. 验收结论

**结论：暂不通过完整验收。项目可以运行，A 股和港股主流程能够生成报告，但当前尚不满足交接文档要求的“计算正确、证据对应、缺口透明、安全可用”。**

原有 58 项测试全部通过，这一点已复核；但它们没有充分覆盖完整指标计算、规则触发后的证据绑定、模型失败、任务期限及真实前端交互。针对代码审查发现的薄弱路径，补充的 53 个测试方法中，7 个通过、46 个未通过；其中 45 个方法发生断言失败、1 个发生运行时异常。这是一组**有针对性的缺陷复现测试**，不能把其失败比例当成项目整体缺陷率或全市场准确率。

本报告将相关缺陷归并为 **31 组问题：21 组 P1、10 组 P2**。P1 表示影响结论、身份、证据真实性、安全或主流程可靠性，应优先修复；P2 表示影响状态、可复现性、数据记录或使用体验。未发现并证实需要按 P0 定性的生产事故；这不改变验收不通过的结论。HANDOVER 第 9 节明确把 XSS/SSRF 绕过列为“不通过”，本轮两类均已在受控环境复现。

最应优先处理的事实是：

- Git 提交中遗漏整个 `app/data/`，新检出代码无法启动。
- 非标准审计意见进入正式规则时发生 `AttributeError`，被降成数据不足；原有万科报告中已有该错误。
- 负利润的现金流背离判断方向写反；全部现金受限时反而恢复账面现金；应收重要性过滤不生效。
- 腾讯原始财报以人民币列示，报告却标为港币；其现金及等价物还被再次扣除单列的受限现金。
- “证据存在”只比对前 40 个非空白字符，不检查完整片段和指纹；同页不同主题证据还会相互覆盖。
- 缺少关键资料、模型解释失败等信息未呈现在 HTML 中；没有形成任何判断也可显示 100 分 A 级。

## 2. 已执行的检查与结果

| 检查 | 实际结果 | 证据/边界 |
|---|---|---|
| 原有离线测试 | **58/58 通过** | Python 3.13.12；约 0.13 秒 |
| 补充缺陷复现 | **53 个方法，7 通过，46 未通过** | 全离线；外部请求替身；临时 SQLite/文件目录 |
| A 股真实扫描 | **生成部分完成报告** | `600519.SH`；168.46 秒；94/94 公告；25 份 PDF；63/63 证据通过项目自身复核 |
| 港股真实扫描 | **生成部分完成报告** | `00700.HK`；111.42 秒；168/168 公告；25 份 PDF；44/44 证据通过项目自身复核 |
| 真实 LLM 调用 | 两次扫描累计约 **0.046 元** | 应用按本机配置单价估算，不是供应商账单；茅台异常解读整批被截断 |
| 历史报告检查 | **17/17 可重渲染；17/17 JSON 与 SQLite payload 一致** | 共 7 个证券、多次历史版本；不是 17 家独立样本 |
| 页面与 API 基础路由 | 正常页面 200；不存在的任务/报告 404 | 首页、历史、管理、进度、报告、下载、健康接口 |
| 在线/下载一致性 | **字节不相同** | 兆易创新样本仅页脚生成时间不同；去掉该时间后相同 |
| 5 个并行任务 | **隔离的 5 个流水线均落库和生成报告，未出现 SQLite 锁错误** | 数据获取及 LLM 使用替身；不是 5 个真实网络/模型满载压测 |
| 行业路由 | 普通、银行、保险、券商、地产均执行 52 项；已配置的特殊行业排除规则生效 | 构造样本；保险/券商未做新的真实全链路扫描 |
| 原始 PDF 抽核 | 已检查腾讯、万科的具体财报页及证据页 | 文本抽取 + 页面图片人工核对 |
| 前端交互 | 复现旧证券被提交、搜索候选 XSS、手机表格挤压、锚点未自动展开证据 | 真实浏览器；危险输入只来自隔离测试服务 |
| 故障与安全 | 直接 IPv4 内网拒绝、文件上限、无密钥跳过、部分 LLM 批次保留、事务回滚通过 | 重定向/IPv6 绕过、空 PDF、超时等失败路径详见下文 |

说明：本次真实扫描刷新了资料索引和结构化数据，但复用了已有 PDF 的隔离副本，不是完全冷缓存首次下载。63/63、44/44 是项目自己的前缀校验统计，**不能据此认定所有结论的证据语义均正确**。

### 环境与复现入口

当前终端没有 `python` 别名；Homebrew Python 3.14 没有安装项目所需的 Jinja2。验收使用与原有服务相同的 WorkBuddy Python 3.13.12，未改动系统 Python 环境。

在项目根目录执行：

```bash
# 原有测试，应成功
/Users/shixiong/.workbuddy/binaries/python/envs/default/bin/python tests/run_tests.py

# 本轮补充测试：当前应失败，修复后逐项变绿；不调用真实网络或模型
/Users/shixiong/.workbuddy/binaries/python/envs/default/bin/python docs/acceptance-2026-09-06/regression_checks.py

# 只复现一项，例如非标审计规则
/Users/shixiong/.workbuddy/binaries/python/envs/default/bin/python -m unittest discover \
  -s docs/acceptance-2026-09-06 -p regression_checks.py -k test_06 -v
```

测试原始日志使用 unittest 的子用例计数方式，显示的 `failures` 数会高于失败的方法数。方法级统计以 [regression_results.json](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/regression_results.json) 为准。

## 3. 功能验收矩阵

| 模块 | 判定 | 主要原因 |
|---|---|---|
| 安装与交付完整性 | 不通过 | 数据适配源码未提交，干净检出无法运行 |
| 证券查询与确认 | 不通过 | 已选值未清空；同市场模糊结果自动选择；非公司证券与错误后缀缺乏严格阻断 |
| A 股/港股资料获取 | 部分通过 | 两市场可取索引和财务数据；源字段缺失、截断、模型/PDF 失败披露不完整 |
| 财务标准化与指标 | 不通过 | 符号、币种、受限现金、年度匹配及财务范围问题 |
| 通用 36 项规则 | 不通过 | 非标审计路径异常、应收过滤失效、公告分类误报/漏报、缺失数据判正常 |
| 行业 16 项规则 | 部分通过 | 路由和显式排除生效；地产公式错误；多项监管指标是固定数据不足 |
| 原文证据与独立核验 | 不通过 | 前缀校验、ID 覆盖、主题/数值对应不足、模型输出引用缺乏验证 |
| LLM 解释 | 部分通过 | 无密钥正确跳过；部分批次失败能保留成功结果；预算/开关/失败展示仍有缺陷 |
| 报告生成与下载 | 部分通过 | HTML 可生成、自包含；关键缺口不展示，时间和严格模板承诺不一致 |
| 任务管理与持久化 | 部分通过 | 5 路隔离并行和事务回滚通过；完成状态、重复提交、超时、记录保真有问题 |
| 安全 | 不通过 | 搜索候选 XSS；下载客户端重定向与 IPv6 内网阻断缺口 |
| 移动端与打印 | 部分验证 | 手机表格可打开但严重挤压；打印展开事件有实现，未实际完成打印输出验收 |

## 4. P1 问题：修复前不应签收

### A01：数据适配层没有进入版本库，交付无法复现

**位置**：[.gitignore](/Users/shixiong/Developer/pailei/.gitignore:2)。

`data/` 未限定为仓库根目录，会同时忽略 `app/data/`。`git ls-files app/data` 返回空；在临时目录中导出 HEAD 并执行 `import app.main`，确定得到 `ModuleNotFoundError: No module named 'app.data'`。本机能启动依赖未被提交的五个适配器及包初始化文件。

**复验标准**：从版本库全新检出、按依赖清单安装后，应能导入应用并跑原有测试。应核对整个适配目录实际入库，不能仅在本机修改忽略规则。当前已有 `.gitignore` 未提交修改在验收开始前就存在，本轮没有更改它。

### A02：HTTP 客户端的内网访问保护可绕过

**位置**：[http_client.py](/Users/shixiong/Developer/pailei/app/core/http_client.py:90)、[自动重定向设置](/Users/shixiong/Developer/pailei/app/core/http_client.py:118)。测试 `23、24`。

初始 URL 经过检查，但客户端自动跟随 302，没有逐跳检查目标。MockTransport 中，公共地址返回指向 `127.0.0.1` 的 Location，客户端确实继续访问了内网目标。IPv4 映射 IPv6 地址 `::ffff:127.0.0.1` 也未被当前网段清单拒绝。直接 IPv4 `127.0.0.1` 拦截正常。

**影响边界**：网页未直接开放任意 URL 下载；风险入口主要是上游披露链接/重定向被控制的情形。本次没有访问真实内网服务，只用本地传输替身验证。请求和下载共用自动重定向客户端，均需修正；DNS 校验与实际连接分离也应一并检查。

**复验标准**：每次重定向均重新检查；映射地址正规化后按非公网地址拒绝；不能把已检查域名重新解析到未检查的地址。

### A03：搜索候选项存在 DOM XSS

**位置**：[home.html](/Users/shixiong/Developer/pailei/app/web/templates/home.html:94)。

候选接口的 `name/secucode/exchange/match_reason` 被直接拼入 `innerHTML`。隔离服务返回包含无害 `img onerror` 的名称，浏览器实际执行，并把 `document.body.dataset.auditXss` 改为 `executed`。见 [浏览器证据](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/search-xss.json)。

报告模板里的 Jinja2 自动转义不能保护这段前端拼接逻辑。这里证实的是**不可信候选响应可执行脚本**，并未声称普通用户输入任意搜索词就能直接反射执行。

**复验标准**：候选文本使用文本节点呈现；相同测试响应显示为文字，不执行事件，不创建可执行标签。

### A04：界面输入与实际扫描证券可能不一致，后端身份校验也不充分

**位置**：[首页输入/提交](/Users/shixiong/Developer/pailei/app/web/templates/home.html:81)、[IdentityResolver](/Users/shixiong/Developer/pailei/app/data/identity.py:162)。测试 `38、39、40` + 浏览器复现。

浏览器步骤：点击“贵州茅台 600519”示例 → 手动把输入改成 `00700` → 点击开始扫描。页面输入是腾讯代码，实际创建的任务 query 仍是 `600519`。原因是 input 事件不重置 `picked`，提交时优先使用旧值。见 [提交记录](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/wrong-security-submission.txt)。后台替身只记录参数，没有扫描错误股票。

后端另有三种已用接口返回替身验证的情况：同一市场多个模糊匹配自动选第一项；`Classify=Fund` 的数字代码可被当成 A 股；显式 `.SH` 后缀不与实际查到的 `.SZ` 公司校验。`a_profile` 只接收数字代码并尝试多市场，进一步掩盖后缀错误。

**复验标准**：修改/清空输入即解除旧选择；模糊候选必须确认；非公司证券拒绝；显式代码的市场必须与主数据完全匹配。后端测试是构造响应，不等于已验证所有真实基金搜索结果。

### A05：亏损公司的利润与现金流背离判断写反

**位置**：[FQ01](/Users/shixiong/Developer/pailei/app/engine/rules/general.py:49)。测试 `03、04`。

仅凭 `ocf / net_profit < 0` 就输出“净利润为正而经营现金流为负”，没有先检查两者符号。净利润 -100、经营现金流 +50，会得到上述相反说明；两者均为负且商大于 0.5，又会得到“现金流覆盖正常”。

真实万科报告 `4df75f7c244b` 已存在：净利润 **-149.51 亿元**、经营现金流 **+4.95 亿元**，FQ01 却按反向情形扣高风险分。其他亏损规则命中不能抵消这一条错误解释和重复扣分。

**复验标准**：覆盖正正、正负、负正、负负、零及缺失的组合；只有实际符合条件才使用“盈利但现金流为负”文案。

### A06：应收账款重要性过滤实际未生效

**位置**：[FQ03 调用](/Users/shixiong/Developer/pailei/app/engine/rules/general.py:105)、[指标生成](/Users/shixiong/Developer/pailei/app/engine/metrics.py:243)。测试 `05`。

过滤函数读取 `accounts_receivable_to_assets`，指标实际叫 `ar_to_assets`，因此返回无法判断并跳过过滤。构造应收仅占资产 0.1%、应收从 0.1 增至 1、收入不变的样本，仍被判风险。原有 TestMateriality 只手工计算比例，没有调用 FQ03，无法保护这一回归。

**复验标准**：由 `compute_metrics → Rule.evaluate` 全链路验证，低于 2% 的应收按现有产品规则不因小基数增速形成风险，达到门槛时正常判定。

### A07：可用现金为零时被回退成全部账面现金

**位置**：[metrics.py](/Users/shixiong/Developer/pailei/app/engine/metrics.py:219)、[SV01 文案](/Users/shixiong/Developer/pailei/app/engine/rules/general.py:295)。测试 `01`。

`usable_cash or cash` 将合法零值当作缺失。现金 100、受限现金 100、短借 50，本应现金覆盖 0 倍，实际 2 倍，并可判正常；SV01 展示金额也有相同回退。

**复验标准**：仅 `None` 才允许缺失处理；零值保留。现金全部受限时不得给出可覆盖短债的结论。

### A08：部分缺失数据被当成零或当成正常，产生确定性结论

**位置**：[SV07](/Users/shixiong/Developer/pailei/app/engine/rules/general.py:382)、[OP04](/Users/shixiong/Developer/pailei/app/engine/rules/general.py:721)、[RE01](/Users/shixiong/Developer/pailei/app/engine/rules/industry.py:196)。测试 `02、21、22`。

三条独立复现：受限资金缺失时输出“受限资金 —，占货币资金 0.00%，占比不高”；有商誉但无总资产时输出“商誉占比不高”；地产没有预收字段时按 0 剔除并给出监管风险结论。原有 A 股报告已可直接看到第一种情况。

**复验标准**：缺少支撑判断的字段必须进入数据不足；区分“披露为 0”和“没有获取到”。非零字段缺失不得用语言掩盖。

### A09：港股报表币种与现金科目口径错误，真实腾讯样本已受影响

**位置**：[hk_currency](/Users/shixiong/Developer/pailei/app/data/eastmoney.py:357)、[HK_BALANCE_MAP](/Users/shixiong/Developer/pailei/app/data/eastmoney.py:107)、[usable_cash](/Users/shixiong/Developer/pailei/app/engine/metrics.py:216)。

腾讯本次报告和历史报告均写 `HKD`。但下载的《中期報告 2026》PDF 第 5 页清楚标明“人民幣百萬元”，收入 401,243、归属股东盈利 114,115，与系统金额逐项对应，未进行汇率转换。原因是直接信任主要指标接口的 `CURRENCY=HKD`，且报告页使用证券资料币种，未和原文报表币种核对。

同份 PDF 第 27 页将**受限制现金 7,729**与**现金及现金等价物 206,930**分行列示；系统把后者映射成通用“货币资金”，再扣 7,729，得到 199,201。该处理重复扣除了不在现金等价物中的单列受限现金，改变现金覆盖指标。

证据：[币种页图片](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/tencent-currency-page5.png)、[现金资产表图片](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/tencent-cash-page27.png)、[当日接口字段](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/source_fields.json)、[披露易原文](https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0825/2026082500557_c.pdf)。这里指 PDF 物理页码，对应印刷页 4、26。

**复验标准**：证券交易币种与财报计价币种分开；使用原文交叉验证；明确现金科目是否包含受限资金，再决定扣除。不能机械把所有港股“现金及等价物”视为 A 股货币资金总额。

### A10：地产“剔除预收后的负债率”公式与其宣称口径不一致

**位置**：[RE01](/Users/shixiong/Developer/pailei/app/engine/rules/industry.py:196)。测试 `49`。

当前公式为 `(总负债 - 预收款项) / 总资产`，分母未剔除预收，且忽略合同负债，却据此使用行业监管区间的表述。万科官网披露文件给出的调整后口径是 `(负债总额 - 预收款项 - 合同负债) / (资产总额 - 预收款项 - 合同负债)`。[官网披露公式](https://www.vanke.com/upload/file/2024-06-17/a2b4e7b4-948e-471f-99b5-c7593287a9b5.PDF)

简单复现：资产 100、负债 80、预收 20、无合同负债，当前为 60% 并判正常，而调整后为 75%。真实万科原文第 87 页同时列出预收款项约 18.39 亿元和合同负债约 775.53 亿元，当前只处理前者。见 [原始资产负债表](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/vanke-financial-page87.png)。

**复验标准**：先固定指标名称、公式和适用口径；数值、说明、阈值保持一致；合同负债未取到时不能宣称完成该口径的核验。这里核对的是指标定义，不把历史监管政策自动视为当前适用于所有公司的约束。

### A11：非标审计意见进入正式规则后触发异常，关键风险被漏报

**位置**：[OP02](/Users/shixiong/Developer/pailei/app/engine/rules/general.py:686)、[RuleContext 字段](/Users/shixiong/Developer/pailei/app/engine/rules/base.py:100)。测试 `06`。

RuleContext 定义 `pending_evidence`，OP02 却写 `ctx._pending_evidence`。只要审计扫描命中，就抛出 AttributeError，被 Rule.evaluate 捕获后改成“数据不足，无法判断”。用“我们无法表示意见”的有效原文输入：独立扫描函数有命中，正式规则却没有风险结果。

真实万科报告 `4df75f7c244b` 的 OP02 已记录“规则执行异常：AttributeError”；HTML 又过滤了数据不足项，用户甚至看不到该执行异常。

**复验标准**：从正式规则执行到证据绑定、原文复核、JSON、HTML 的全链路验证命中，不只测文本检索函数。

### A12：审计识别仍有繁体漏检、标准无保留误报、空正文判正常

**位置**：[scan_audit_opinions](/Users/shixiong/Developer/pailei/app/data/pdftext.py:241)、[OP02 scanned 选择](/Users/shixiong/Developer/pailei/app/engine/rules/general.py:670)。测试 `07、08、52`。

“我們無法表示意見”未命中简体正则；“出具了标准无保留意见”却可被断言式正则中的“保留意见”命中，因为否定检查只看整个匹配之前。另有一页空文本的 ParsedDoc 会被计为已扫描，输出未检出异常。

**复验标准**：简繁体均覆盖；标准无保留与非标准意见区分；空白、扫描件、解析失败、未解析到意见段不能作为正常依据。修复 A11 后更应修复本项，否则原来被异常遮住的误报会重新出现。

### A13：公告分类的先后顺序及否定/解除语义导致漏报和误报

**位置**：[A 股分类](/Users/shixiong/Developer/pailei/app/data/cninfo.py:221)、[港股分类](/Users/shixiong/Developer/pailei/app/data/hkexnews.py:235)。测试 `13、14、15`。

确定性复现：

| 输入标题 | 当前分类/后果 | 应保留的含义 |
|---|---|---|
| 关于2025年年度报告的更正公告 | 年报；FQ12 漏掉更正 | 财务更正 |
| 关于2025年年度报告的问询函 | 年报；RG03 漏掉问询 | 监管问询 |
| 关于聘任会计师事务所的公告 | 高管变动 | 审计机构 |
| 2026年半年度业绩预告 | 其他公告 | 业绩预告 |
| 盈利警告 - 預期年度業績錄得虧損 | 业绩 | 盈利警告 |
| 关于公司全部资产解除冻结的公告 | 资产冻结；RG05 直接高风险 | 已解除事件 |

真实兆易创新报告把“聘任境外会计师事务所”作为高管变动，模型解释已经指出误匹配，但状态及扣分仍保留。承诺“不减持”的公告也被纳入减持计数。当前只按类型触发的规则不能正确区分当前风险和解除进展；这不是要求扩展跨年追溯，而是正确理解窗口内已有标题。

**复验标准**：风险特征优先于泛化“年度报告/業績”；覆盖否定、解除、终止、续聘、例行文件及真正变更的成对样本。

### A14：原文复核只比较前 40 字，片段后半段与指纹均可伪造

**位置**：[verify_evidence](/Users/shixiong/Developer/pailei/app/data/pdftext.py:146)。测试 `09`。

把真实原文前缀保留，将后半段替换为不存在的债务违约断言，并设置错误指纹，函数仍返回 `verified=True`。它只查找去空白后的前 40 字，没有核对剩余引文，也不校验 fingerprint。

**复验标准**：核验完整规范化片段、文档身份、物理页码和指纹。前缀相同、尾部金额不同、跨页拼接、错误指纹都必须失败。真实语义复核另见 A16，不能与字符串存在性混为一谈。

### A15：同页证据相互覆盖，精确定位证据的再次核验又找错文档

**位置**：[证据 ID 生成](/Users/shixiong/Developer/pailei/app/data/pdftext.py:132)、[EvidenceStore.add](/Users/shixiong/Developer/pailei/app/engine/rules/base.py:48)、[流水线复核](/Users/shixiong/Developer/pailei/app/engine/pipeline.py:424)。测试 `10、11`。

所有同页片段共用 `doc_id:p页码`。先添加应收主题、后添加同页存货主题，会覆盖实体，但应收主题索引仍指向该 ID，最终取出的“应收证据”已变成存货片段。

另一条路径生成 `doc_id:loc指纹`，复核却用 `evidence_id.split(':p')[0]` 查找文档，忽略已有 `ev.doc_id`。因此即使真实精确片段存在，也被标“未找到原文解析结果”。

**复验标准**：同页不同片段具有独立稳定 ID；所有主题索引与实体一致；使用独立文档标识查找原文；`p/loc/meta` 路径分别验证。

### A16：按主题/文档类型找来的片段，不能自动认定为“原始披露确认”

**位置**：[attach_evidence](/Users/shixiong/Developer/pailei/app/engine/runner.py:116)。测试 `12` + 真实报告/PDF 抽核。

只要风险项绑定了任意 evidence_id，就设置 `EvidenceStrength.CONFIRMED`，没有验证金额、期间、主体和结论是否对应。构造“2020年度报告，公司通讯地址”即可被绑定到 2026 年亏损结论，并标为原始披露确认。

真实万科 FQ03“应收同比增速差”引用的 PDF 第 58 页，是对外担保/抵质押表，并不提供计算该同比所需的应收余额和收入比较；另一引用是股东借款公告。SV04“-3.08 倍”也引用股东借款公告片段，不能从所展示原文独立复算。见 [第 58 页图片](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/vanke-evidence-page58.png)。

**复验标准**：数值型结论绑定所需科目、期间和数值所在页；事件型结论绑定该具体事件；只有语义对应才提升证据强度。无证据、弱证据、模型未核验的情形应如实表达。

### A17：报告隐藏实际缺口，且在零判断时给出 100A 的宽慰性文字

**位置**：[report.html.j2](/Users/shixiong/Developer/pailei/app/report/templates/report.html.j2:155)、[AI 显示](/Users/shixiong/Developer/pailei/app/report/templates/report.html.j2:290)。测试 `26、27` + 真实扫描。

上下文提供了 gaps、missing_data、coverage、ai.notes、ai.verification、method.limitations，但当前四节模板不展示实际缺口、覆盖比例或核验失败原因。只有存在风险/关注时才显示数据不足数量；没有风险时连这个数量也消失。构造 0/36 已判断，仍显示“100 A、未发现明显风险信号”。本项不是反对已经加入的评分功能，而是指出未覆盖被呈现成确定的宽慰性结果。

本次茅台扫描 3 项异常的 LLM 解释全部因 `max_tokens` 截断未生效；JSON 如实记录失败，但 HTML 只说 AI 已启用、3 次调用。规则卡片又说“规则模板（未启用模型解读）”，把调用失败混成没启用。腾讯实际 5 项数据不足，因风险和关注都为 0，HTML 没有数据不足数量。

下载份数/解析页数限制在配置中存在，但具体未下载文件和未解析页区间没有明确展示；PDF 的 `truncated` 未进入缺口。万科年报 298 页、腾讯年报 282 页却最多解析 120 页，不能因为“下载了文件”就表示完整检查。

**复验标准**：保留当前四节结构也可以，但关键缺口、有效覆盖、实际 AI 状态和核验失败必须可见；零判断不得显示确定的无明显风险结论；未完成项隐藏这一产品决定不能同时隐藏关键数据失效。

### A18：LLM 返回不存在的公告 ID 也会进入事件结果

**位置**：[_extract_events](/Users/shixiong/Developer/pailei/app/engine/pipeline.py:401)。测试 `31`。

模型事件只检查 doc_id 非空及不在已有事件中，没有检查它属于本次下载的 docs 或本批上下文。替身返回 `doc_id=nonexistent`，会创建 RiskEvent 并继续落库，形成无法关联的来源。

**复验标准**：模型所有 ID 都按本次输入白名单校验；事件主体验证、日期和引用片段有程序约束。不存在 ID 的结果应拒收并记录原因。当前时间线未在 HTML 展示，不代表 JSON/数据库中的错误可以接受；这里没有进行对真实模型的攻击测试。

### A19：启用模型时一个 PDF 解析失败可以导致整次扫描失败

**位置**：[pipeline.py](/Users/shixiong/Developer/pailei/app/engine/pipeline.py:390)。测试 `32`，实际为 `IndexError`。

构造存在于 parsed 字典、但 pages 为空且带 parse_error 的文件，事件上下文直接读取 `pages[0][1]`。主流水线顶层捕获后变为失败，无法生成本来承诺的带缺口部分报告。已有其它正常文件也不能避免该路径。

**复验标准**：失败/无文本文件进入明确缺口，跳过文本读取；其余有效资料仍可生成部分报告。模型启用、关闭两种模式都应验证。

### A20：超时不是执行期限，过期后仍继续财务抓取/模型步骤

**位置**：[pipeline.py](/Users/shixiong/Developer/pailei/app/engine/pipeline.py:228)、[下载时间检查](/Users/shixiong/Developer/pailei/app/engine/pipeline.py:292)、[parse_pdf](/Users/shixiong/Developer/pailei/app/data/pdftext.py:45)。测试 `42`。

deadline 主要只用于下载循环前及最终标注。把任务期限设成已经过去，后续 `a_statements` 仍被调用；PDF 解析没有独立时限，公告分页和 LLM 步骤也未按剩余时间统一截断。`timed_out=True` 是事后记录，不能保证约 15 分钟内结束。

**复验标准**：每个耗时阶段共享剩余期限，HTTP/LLM 超时不大于允许时间；PDF 解析有可终止边界；到期结束为带缺口报告或明确失败，不无限占用 5 个工作线程。本轮用替身复现了过期后仍执行，没有等待真实任务卡死 15 分钟。

### A21：同比和事实索引没有完整保证同期、同币种、同范围

**位置**：[compute_metrics prior](/Users/shixiong/Developer/pailei/app/engine/metrics.py:124)、[FactSet 索引](/Users/shixiong/Developer/pailei/app/engine/normalize.py:42)。测试 `16、17、19`。

相同 PeriodType 下取第二个日期，不确认它是上年同期：缺少 2025 中报时，2026 对 2024 也会被叫“同比”。同一时期的 HKD 与 CNY 数值无检查即可计算同比。索引键只有 `(std_item,period_end)`，后插入的母公司收入可覆盖同日期的合并收入；公告日期、重述优先级也未参与选择。

**复验标准**：明确上年同一期间；币种、范围、累计口径一致，不一致须标记无法比较；适用的重述版本按规则选取，不能依赖列表最后一条。上述冲突场景为构造样本，并未断言所有现存事实已经混用。

## 5. P2 问题：状态、记录与交付体验

### A22：所有有财报的任务都会“部分完成”，60 分钟复用和重复提交控制失效

**位置**：[gaps 写入](/Users/shixiong/Developer/pailei/app/engine/pipeline.py:199)、[终态判断](/Users/shixiong/Developer/pailei/app/engine/pipeline.py:254)、[find_recent_task](/Users/shixiong/Developer/pailei/app/core/db.py:261)。测试 `41、50`。

metrics.notes 无条件包含“计算基准”说明，流水线把它当缺口，因此 `self.gaps` 始终非空。隔离掉其它缺失、所有适用规则正常时，仍为部分完成。原始数据库中 17 个成功出报告任务全是部分完成、没有“完成”。缓存却只匹配 status='完成'，实际没有可复用结果。

同一查询尚在排队/运行时也不去重；两次提交生成不同 task_id。线程数为 5 不等于队列有界，持续点击/调用可积压更多任务。

**复验标准**：普通说明与真正缺口分开；定义哪些部分报告可复用；并发重复提交原子地复用在途任务；强制刷新仍按明确规则执行。

### A23：功能开关不生效，预算中止和失败信息不完整

**位置**：[LLMAdapter.available](/Users/shixiong/Developer/pailei/app/llm/adapter.py:62)、[分批预算](/Users/shixiong/Developer/pailei/app/llm/adapter.py:178)、[HTTP 请求](/Users/shixiong/Developer/pailei/app/core/http_client.py:148)。测试 `28、29、30`。

`.env.example` 提供的 `LLM_ENABLED=false` 没有被 available 检查，仍可能调用模型；`ENABLE_NETWORK=false` 没有在 HTTP/流水线入口使用，仍会发请求。分批已成功一批后预算不足，直接 break 返回 ok=True，没有把取消的剩余批次记为失败/跳过；构造预算 0.35、首批消耗 0.1、下一批保守估计 0.3 即复现。

预算估计固定 0.3，不依据输入量、max_tokens 和已配置价格保留调用上限；不宜把它描述为严格费用硬上限。直接 chat_json 也没有预算检查。

**复验标准**：公开配置开关确实控制执行；预算中止保留成功结果，同时显示未完成数量和原因；说明预算估计/硬限制的实际保证范围。

### A24：AI 下调结论后没有重算覆盖统计

**位置**：[ai_verify](/Users/shixiong/Developer/pailei/app/engine/runner.py:261)。测试 `33`。

coverage 在 run_rules 中算好；ai_verify 把 RISK 下调成 INSUFFICIENT 后只改 outcome，覆盖数不变。最终 summary.insufficient_count 与 coverage.insufficient/evaluated 可互相矛盾。

**复验标准**：最后一次规则状态变更后统一重算摘要、维度统计、覆盖数、排序与评分输入，不能混用前后两个阶段的状态。

### A25：历史窗口少一个完整财年；部分指标单位和财年起始日不准确

**位置**：[_limit_years](/Users/shixiong/Developer/pailei/app/data/eastmoney.py:556)、[_hk_indicators](/Users/shixiong/Developer/pailei/app/data/eastmoney.py:369)、[_period_start](/Users/shixiong/Developer/pailei/app/data/eastmoney.py:547)。测试 `18、20`。

设置回看 5 个完整财年时，将当前 2026 年也占一个名额，保留 2022—2026，丢掉应保留的 2021 完整财年。构造五个年报加一个当前中报，只剩 5 条。港股 ROE/ROA 的百分数值直接当“比率”，当日 ROE=9.966353785254 未标准化为 0.09966353785254；目前这两个指标不直接触发规则，影响主要在结构化结果及以后消费该结果的组件。

`_period_start` 还把所有年报/中报按 1 月 1 日开始，Q1 则等于期末日，不适合非自然财年；部分 HK 行有 START_DATE 可以保留，但其它路径仍错误回退。

**复验标准**：5 个完整财年另加当前年；百分数/比率/金额分别标准化；报告开始日以原始期间和财年为准。

### A26：财务口径元数据及行业归属未完整落库

**位置**：[save_facts](/Users/shixiong/Developer/pailei/app/core/db.py:283)、[RuleOutcome.to_result](/Users/shixiong/Developer/pailei/app/engine/rules/base.py:220)。测试 `34、36`。

FinancialFact 有 period_start、audited、consolidated，但 financial_facts 表没有保存它们；报告 JSON 也没有保存逐条原始 fact 对象，因此不能仅靠保存的结果完整重建这些口径。RuleOutcome 在 run_rules 被赋行业包，但 to_result 写死 `general`，银行 BK01 入库也成为 general。

**复验标准**：内存→数据库→读取能保留口径；行业字段与实际选择一致；对外声称“完整落库”的字段逐一验证。不能只看 dataclass 定义就判断持久化完成。

### A27：获取日志被重复累计，读连接未明确关闭

**位置**：[save_fetch_logs](/Users/shixiong/Developer/pailei/app/core/db.py:414)、[get_task/connect](/Users/shixiong/Developer/pailei/app/core/db.py:190)。测试 `35、51`。

流水线多次把整个不断增长的 records 列表插入，不是只插入增量。真实新扫描中，茅台 HTTP 记录 12 条，fetch_logs 有 35 行；腾讯 28 条，入库 67 行，source_health 次数及流量同步重复累加。

`with sqlite3.Connection` 负责提交/回滚，不负责 close。get_task 等读函数结束后连接仍可执行 SQL，验收日志也出现 Python 3.13 的 unclosed database ResourceWarning。当前事务回滚测试通过，5 路隔离并发也通过，不能把这一发现夸大成“SQLite 已经无法并发”。

**复验标准**：一个真实请求仅记一次；增量写入/请求 ID 去重；读写均明确管理连接关闭，在持续轮询下资源不增长。

### A28：StrictUndefined 承诺与实际配置不符

**位置**：[_env](/Users/shixiong/Developer/pailei/app/report/render.py:118)。测试 `25`。

仅导入 StrictUndefined，没有传入 Environment。运行时是默认 Undefined，会将部分缺失字段吞成空字符串；交接文档所述“未定义变量直接报错”不成立。模板还读取 usage.model，但 usage_summary 没有返回这个字段，正被默认容错掩盖。

**复验标准**：模板与 payload 契约一致，缺少必填字段有受控错误；补齐现存缺失项后再启用严格校验，不能只改一个参数后让正常报告全部报错。

### A29：在线报告与下载并非同一不可变产物，生成时间会变化

**位置**：[在线/下载路由](/Users/shixiong/Developer/pailei/app/main.py:122)、[rendered_at](/Users/shixiong/Developer/pailei/app/report/render.py:202)、[页脚](/Users/shixiong/Developer/pailei/app/report/templates/report.html.j2:298)。

在线每次实时渲染 SQLite payload，下载读磁盘旧 HTML；页脚“生成于”用当前渲染时间。兆易创新样本字节校验不同，去掉页脚时间后才一致。本轮这个样本没有发现正文差异，但模板日后更新仍可能造成在线/下载内容版本不一致。

**复验标准**：报告生成时间固定为报告版本时间；在线和下载使用同一版本产物或明确版本化渲染。旧 JSON、SQLite 和 HTML 的唯一事实源约定应可验证。

### A30：手机检查表过度挤压，锚点不会自动展开目标证据

**位置**：[表格/徽章样式](/Users/shixiong/Developer/pailei/app/report/templates/report.html.j2:43)、[openHashTarget](/Users/shixiong/Developer/pailei/app/report/templates/report.html.j2:311)。

390×844 浏览器截图中，“已覆盖资料中未发现明显异常”徽章强制不换行，挤占说明列，使金额、说明和检查项逐字竖排，阅读成本很高。见 [手机截图](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/mobile-390.png)。该尺寸未测得整页横向溢出，不把它误报为横向滚动故障；实际问题是列宽和可读性。

点击风险目录中的“审计机构变更”后 URL 已到 `#ev-GV01`，但该卡片的 details.open 仍为 false。函数只遍历目标的父元素，而 details 是目标卡片的子元素。手工点击展开原文仍可用。

**复验标准**：390/640/桌面均能连续阅读数字和句子；锚点打开目标卡片内部的相关折叠区，实际点击后验 DOM 状态。

### A31：非对象 JSON 请求导致 500，API 缺乏输入契约

**位置**：[create_scan](/Users/shixiong/Developer/pailei/app/main.py:176)。测试 `37`。

JSON 解析成功后立即 `body.get`，对 `[]`、`null`、`42`、字符串均返回 500。另有 force 使用普通真值转换、query 任意类型字符串化，难以稳定区分非法请求和合法参数。

**复验标准**：请求字段有明确类型、长度和空值约束，坏请求返回 400/422，不能制造运行时异常或扫描不存在的序列化对象。

## 6. 已知边界与交付文档差异，不混算为本轮新 bug

1. 登录/邀请、S3、Docker Compose、Celery/Redis/PostgreSQL、跨年追溯、20 家固定标注回归集、商业授权，DEVELOPMENT_STATUS 已标明未做/未核验。本轮不要求增补这些功能，也不把它们计入上述 31 组；但因此仍不能按原始 V1 完整交付范围验收。
2. 本地线程池+SQLite、四节报告以及新增风险信号评分，按当前交接版本检查。原方案“不输出安全分数”和后续评分方案之间的产品口径需要负责人明确；本轮诊断重点是已有评分输入与展示的正确性，没有提出新增或删除评分功能。
3. BK03/BK04、IN03/IN04、BR03/BR04 是固定返回数据不足；这些行业规则的“存在”不等于监管风险已被有效覆盖。按已声明限制记录，不能把 52 个注册项当作 52 个有效完成的检查。
4. 当日实际 `RPT_DMSK_FN_BALANCE` 响应不包含流动资产/负债合计、长期借款、合同负债等完整报表字段，部分 `SHORT_LOAN` 也为空。这部分不能一概归咎于“漏写字段映射”。原文中有数据但当前适配链没有补足，是实际覆盖边界；真正的新 bug 是仍把缺失解释为正常，或用不完整口径给出确定判断。
5. 原始财务事实来源 URL 主要指向数据接口/报表名，通常不含证券、期间过滤参数，无法单靠链接直接复取同一条事实。报告中部分“所有结论均绑定原文”文案也不符合实际：正常项通常没有绑定、部分风险项绑定不充分。应纠正文档/状态表的完成口径。
6. 趋势图、事件时间线、来源清单的数据和辅助代码仍在，但四节模板没有呈现；README 的部分描述仍承诺这些内容。是否恢复展示属于产品取舍，本轮仅登记文档与交付差异，不擅自补功能。
7. 任务重启后运行中的任务不续跑、不清理为明确终态；文档已承认没有断点续跑。本轮没有重启原有服务做破坏性试验，因此不声称任务恢复通过。

## 7. 为什么“58 项全过”不足以签收

- TestMateriality 自己算应收/资产、存货/资产比例，没有运行实际规则，漏掉 A06。
- 审计测试只验证 `scan_audit_opinions`，没有走 RuleContext→OP02→证据保存，漏掉 A11。
- 币种测试只检查 CNY/HKD 字符串保留，没有和财报计价币种、百分数单位、可比口径核对，漏掉 A09/A21/A25。
- 原有 XSS 测试只测报告模板，未测首页 `innerHTML`；URL 测试只测展示协议，没有下载重定向/IPv6，漏掉 A02/A03。
- 没有验证合法 JSON 的错误结构、运行中重复提交、超时强制收尾、PDF 失败伴随 LLM、AI 下调后的覆盖数。
- 有些原有测试明确接受空结果 100A，这能证明评分算术符合实现，但不能证明缺失数据的解释不会误导。

补充测试保留了 7 个通过的控制项：5 路并行落库、事务回滚、直接 IPv4 内网阻断、LLM 部分批次保留、无密钥不联网、下载体积限制、5 类行业规则路由。报告不是把整个实现都否定；问题集中在主流程串接与财务/证据语义的边界。

## 8. 修复与复验顺序（本轮未实施）

1. **先保证可交付和安全**：A01—A04。干净检出可运行，输入与证券一致，脚本和内网访问被阻断。
2. **再修正会改变判断的错误**：A05—A13、A21。补齐符号组合、零/缺失、币种、现金组成、审计正反样本、公告解除语义和同比期间。
3. **让证据及失败状态可信**：A14—A20。逐条高风险能从其引用页找到判断依据；无效证据不能确认；解析和模型故障可降级；用户看得到缺口。
4. **处理状态与交付保真**：A22—A31。缓存、预算、数据统计、元数据和 HTML 行为一致。
5. **复验通过条件**：原有 58 项与本轮适用的补充测试通过；对修正后万科/腾讯/兆易创新等报告重新抽核；两市场真实端到端运行；记录 AI 未生效/数据不全时的页面；再做真实 5 路并发、重启与打印检查。新的修复不能靠隐藏错误检查项来“通过”。

这不是功能扩展清单。建议修复已有逻辑和呈现，然后针对发现复验；不要在同一批修复中顺带重写架构或扩张规则数量。

## 9. 验收证据目录

| 材料 | 用途 |
|---|---|
| [baseline_results.log](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/baseline_results.log) | 原有 58 项测试原始结果 |
| [regression_checks.py](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/regression_checks.py) | 可独立重跑的 53 项针对性验收测试 |
| [regression_results.json](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/regression_results.json) | 每项成功/失败及原始异常栈 |
| [regression_results.log](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/regression_results.log) | unittest 完整日志 |
| [live_scan.log](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/live_scan.log) | 两市场真实扫描的范围、耗时、缺口、模型状态 |
| [live_scan.py](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/live_scan.py) | 隔离真实扫描入口；会访问数据源和调用已配置模型 |
| [茅台新报告](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/runtime/reports/accept_600519_SH.html) | 本轮新生成的实际产物 |
| [腾讯新报告](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/runtime/reports/accept_00700_HK.html) | 本轮新生成的实际产物 |
| [sample_inspection.json](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/sample_inspection.json) | 17 份历史报告、API 路由、源码完整性和在线/下载比对 |
| [inspect_samples.py](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/inspect_samples.py) | 历史产物检查脚本；读取原数据库，访问原有 8770 服务 |
| [source_fields.json](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/source_fields.json) | 当日财务接口字段和样本值 |
| [browser_fixture.py](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/browser_fixture.py) | 隔离的 8771 UI 测试服务；后台只记录参数，不调用模型/数据源 |
| [search-xss.json](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/search-xss.json) | 候选脚本执行的无害标记 |
| [wrong-security-submission.txt](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/wrong-security-submission.txt) | 修改输入后实际提交旧证券的浏览器记录 |
| [mobile-390.png](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/mobile-390.png) | 手机表格可读性问题 |
| [腾讯币种页](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/tencent-currency-page5.png)、[腾讯现金页](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/tencent-cash-page27.png) | 币种及受限现金口径原始证据 |
| [万科证据页](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/vanke-evidence-page58.png)、[万科财务页](/Users/shixiong/Developer/pailei/docs/acceptance-2026-09-06/vanke-financial-page87.png) | 语义错配、合同负债与报表科目核对 |

`runtime/` 内是隔离数据库、报告及 PDF 副本，已用验收目录自己的 `.gitignore` 排除，避免将运行时大文件提交到项目仓库。日志与本报告未包含模型 API 密钥。

## 10. 尚未验证的范围

没有把以下内容当成通过：20 家固定人工标注集及全市场召回率、北交所新鲜端到端样本、保险/券商新鲜真实扫描、所有港股非自然财年、真实网络/模型 5 路满载、实际进程重启恢复、浏览器实际断网及打印成品、生产权限隔离、商业数据授权、对真实模型的文档指令攻击。

已做离线 HTML 外部资源检查，报告不依赖外部脚本/字体/图片；已检查打印前后展开/恢复的实现，但没有以代码审阅冒充实际打印验收。

**最终判定维持：能运行、能出报告，尚不能签收为已完成质量验收的基本面排雷产品。应先修复现有错误，再对同一组证据和测试复验。**
