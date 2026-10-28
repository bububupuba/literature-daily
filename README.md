# 四方向文献日报（无大模型 API）

每日北京时间 09:00 在 GitHub Actions 运行，微信推送摘要并链接到本公开仓库的完整日报。电脑无需开机。没有新增论文也通知。

## 每日篇数与排序

- **期刊论文最多 25 篇，预印本另外最多 25 篇，总上限 50 篇。**
- 两组分别为线粒体、秀丽隐杆线虫、衰老与长寿、脂质生物学各预分配 5 篇，交叉研究另预分配 5 篇。
- 某栏不足 5 篇，余量在同一组内调剂；不跨期刊／预印本组借额。全部合格候选不足时，按实际数量输出。
- 每篇仅归属一个展示栏目，整份日报去重。所有关联方向仍在详情中列出。
- 先筛选研究相关性。期刊组优先按**已核验期刊影响因子**降序，之后按相关性规则分；预印本按相关性、方法和机制关键词排序，不赋影响因子。
- 影响因子来源、指标年份与核验日期列在详情中。官网只给值而未注明年份时如实注明；未知指标不估算，仍可入选但排序靠后。指标覆盖并不完整。
- 阅读分级为“必读候选／值得一读／其他相关”，依据主题与实验方法关键词阈值，不代表创新性、严谨性或机制深度已经经过评审。

## 数据源与去重

四方向独立查询，不要求同时覆盖四个方向，不设期刊白名单。

- Europe PMC：各方向标题／摘要检索。
- **PubMed**：直接访问 NCBI E-utilities。与 Europe PMC 部分共用文献索引，但访问服务独立，可在其中一条服务路径失效时补充获取题录和摘要。
- **Crossref 期刊查询**：按多组方向词与出版日期直接查出版社登记记录，补充期刊来源。
- **期刊官方 RSS**：Nature、Nature Communications、Nature Cell Biology、Nature Aging、Nature Metabolism、Nature Reviews Molecular Cell Biology、Nature Reviews Endocrinology、Nature Neuroscience、Cell、Molecular Cell、Cell Metabolism、Current Biology、eLife。RSS 是滚动条目，不保证完整回溯。
- bioRxiv／medRxiv 官方接口：新发与修订预印本。
- Research Square：Crossref DOI 前缀和日期增量查询。

按 DOI、PMID、数据库编号、Research Square 版本根 DOI 和明确登记的预印本／期刊关系合并，不靠相似标题强行合并。初次回溯含当天的 3 个日历日，之后重叠检索并保存历史；超过名额的候选顺延。

每个数据源单独报告成功、失败或分页不完整。**任一已启用来源（包括 RSS）重试后仍失败，或分页不完整，整次运行失败：不发布新日报、不覆盖已有日报、不更新推荐历史、不推送微信。** 其他源成功不能替代失败源。数据库收录延迟、缺少摘要、日期不完整及无登记版本关系，仍可能导致漏检。

RSS 请求使用匿名 Cookie 会话并串行间隔访问，响应必须是有效 RSS/RDF/Atom；无效内容最多重试 3 次。`diagnostics/retrieval-YYYY-MM-DD.json` 与 `state/retrieval_status.json` 保存每次尝试的状态、响应类型、正文长度、摘要哈希、去掉查询参数的跳转地址及 XML 错误，不保存 Cookie、凭据或验证参数。不会把 HTML 验证页面当作零篇论文。

## 微信失败补发与故障提示

- 09:00 生成并保存报告，再投递微信；**10:00、12:00 只检查并补发未确认送达的报告**，不重新生成另一份当日日报。
- 如果最近一次检索失败或中断，10:00、12:00 也停止补发；必须修复并成功重跑后才恢复。手动投递同样拒绝检索不完整的旧日报。
- 明确失败的报告保留在待发送队列，下次投递时把漏发日报的完整链接合并进一条消息。
- 成功回执按日报日期记录；同日手动重跑或更新日报不会自动重复发送已经确认的消息。
- 超时、连接中断等可能属于“服务已接收但客户端没收到回执”。这种结果标为**待核实**，不自动连续重发，避免重复通知。
- GitHub Actions 运行摘要与 `state/health.json` 显示发布／推送是否已阻止、数据源问题、未送达日期、需核实日期。检索或投递失败时工作流显示失败，诊断与尝试状态仍保存回仓库。
- 核实 Server酱投递记录后，若确认未收到，可在 Run workflow 勾选 `send_notification` 和 `retry_uncertain` 后重试。`retry_uncertain` 不应在未核实前开启。

## 配置与运行

当前仓库已部署，`DAILY_ENABLED=true` 已开启。推送密钥放在仓库 Actions Secret `SERVERCHAN_SENDKEY`，不写入代码，不发送到聊天。

需要 Python 3.11+，仅使用标准库，不需要大模型 API、数据库 API Key 或第三方 Python 包。

```bash
python -B -m unittest discover -s . -p 'test_*.py' -v
python monitor.py preview --directory validation/preview
python monitor.py build
python monitor.py build --refresh
python monitor.py retry
python monitor.py status
```

`preview` 不写生产历史、不发微信。`build --refresh` 按新规则更新今天的日报和来源状态，保留已报告版本历史，防止旧条目再次推荐。GitHub 手动运行中提供 `refresh_report` 对应此功能。

## 文件

- `config.json`：方向、关键词、期刊／预印本名额、阅读分级阈值、RSS 与 Crossref 查询词。
- `journal_metrics.json`：逐刊核验的影响因子值、年份与官方来源；不完整的指标表，需要随年份更新。
- `monitor.py`：检索调度、筛选、名额分配、去重和报告。
- `sources_extra.py`：PubMed、Crossref 期刊和 RSS 解析。
- `delivery.py`：待发送队列、补发、投递状态和运行健康摘要。
- `reports/YYYY-MM-DD.md` 与 `.json`：完整日报与结构化结果。
- `state/history.json`：候选与已报告版本历史。
- `state/deliveries.json`、`state/delivery_attempts.json`、`state/health.json`：成功回执、尝试状态和故障信息。
- `state/retrieval_status.json` 与 `diagnostics/`：检索与生成完成标志，以及每个来源的可追溯诊断。

## 内容分析边界

中文栏目与筛选理由，原文标题和摘要保留英文。不生成 AI 中文总结，不自动区分每句实验结果／解释／假设。科学意义、实验可靠性和因果机制仍需阅读全文判断。期刊影响因子不能等同单篇论文质量。

## 官方文档

- https://europepmc.org/RestfulWebService
- https://www.ncbi.nlm.nih.gov/books/NBK25499/
- https://www.crossref.org/documentation/retrieve-metadata/rest-api/
- https://api.biorxiv.org/
- https://github.com/easychen/serverchan-sdk
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- NCBI 使用声明与版权说明：https://www.ncbi.nlm.nih.gov/home/about/policies/
