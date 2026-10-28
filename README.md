# 四方向文献日报（无大模型 API）

面向 bububupuba：线粒体生物学、秀丽隐杆线虫、衰老与长寿、脂质生物学分别检索，并生成交叉研究栏目。

## 运行方式

- GitHub Actions 每天北京时间 09:00 触发；调度可能延迟，运行不依赖个人电脑开机。
- 首次回溯含当天的 3 个日历日；后续默认重叠 7 天，未完成检索会扩大回溯补查。
- 每天最多推荐 100 篇独立论文。各方向共享同一论文编号，不重复计数；超过上限的候选顺延。
- 没有新论文也通知；数据源失败时明确显示检索缺口，绝不将失败报告成零新增。
- 微信只显示部分精选标题和完整日报链接；完整日报在本私有仓库中，需要登录 GitHub 阅读。
- 不接入任何大模型服务，不需要模型 API Key。中文栏目、中文匹配理由；原文标题与摘要不自动翻译。

## 先完成这两项配置

1. 在仓库 Settings → Secrets and variables → Actions → Secrets 中添加 `SERVERCHAN_SENDKEY`，值为自己在 Server酱申请的 SendKey。不要放入代码或聊天。
2. 首次运行 Actions → Daily literature → Run workflow，先保持 `send_notification=false`，检查日报；确认后以 `true` 试发微信。
3. 收到通知后，在同一设置页的 Variables 标签中添加 `DAILY_ENABLED=true`，开启日常运行。未设置时，定时任务不会执行。

本仓库工作流使用 GitHub 自动提供的 GITHUB_TOKEN 保存报告和去重历史，不需要个人访问令牌。
私有仓库 Actions 用量计入账号额度；Server酱服务额度以其账户页面为准，不承诺所有服务永久免费。

## 本地验证

需要 Python 3.11 或更高，仅使用标准库。

```bash
python -m unittest discover -s . -p test_monitor.py -v
python monitor.py preview --directory validation/preview
python monitor.py build
python monitor.py notify
```

`preview` 不写生产历史或发送微信。`build` 写入报告与历史；同日重跑保留原报告，便于失败通知重试。`notify` 读取环境变量 SERVERCHAN_SENDKEY，只发送一条摘要卡片；成功回执记录后不重复发当天消息。网络在服务接收后断开时无法实现严格的恰好一次投递，需检查服务历史后再重试。

## 检索与排序

- Europe PMC：四组独立标题/摘要查询，覆盖其索引内的期刊与预印本，不按期刊限制。
- bioRxiv / medRxiv：官方日期接口，包含新发和修订版本。
- Research Square：Crossref 的 DOI 前缀与日期增量接口。
- DOI、PMID、数据库编号、Research Square 版本根 DOI，以及明确登记的预印本/期刊关系用于合并。没有足够关系证据时不凭相似标题强行合并。
- 标题和摘要主题匹配为主要排序依据；实验机制、方法和发现表述提供有限加分。交叉主题适当加分，偏好期刊只用作同分次级排序。
- 每个方向预留最多 15 个候选位置，余下名额全局排序，避免大量脂质或衰老论文挤掉线虫栏目。

## 无模型模式的边界

**本版本不能替代科学评审。** 不声称准确判定创新性、严谨性、科学意义或因果机制；不把摘要中的作者推测写成实验事实。不生成每篇 AI 中文发现总结、意义分析或自动证据分层。预印本明确标记未经同行评审。

规则可能有漏检/误检；索引可能延迟；没有摘要或日期的文献暂不推荐；没有登记关系的预印本转期刊可能漏识别。实际检索成功与否及分页截断均保存在每日日报。引用的五篇代表论文用作需求记录，不作为必须同时满足的主题限制。

## 文件

- `config.json`：方向、关键词、偏好期刊与数量限制。
- `monitor.py`：抓取、筛选、去重、报告和微信推送。
- `reports/YYYY-MM-DD.md`：可直接在 GitHub 阅读的完整日报。
- `reports/YYYY-MM-DD.json`：结构化结果、数据源状态和过滤统计。
- `state/history.json`：候选历史及已报告版本，不要随意删除。
- `state/deliveries.json`：成功推送回执。

## 官方接口文档

- https://europepmc.org/RestfulWebService
- https://api.biorxiv.org/
- https://github.com/CrossRef/rest-api-doc
- https://github.com/easychen/serverchan-sdk
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
