# Zotero-arxiv-daily：独立的指定期刊每日新论文邮件

本补丁为 `XyynBldy/zotero-arxiv-daily` 增加 **一个独立 GitHub Actions Workflow**，不替换原有 Zotero/arXiv AI 推荐邮件。它仅采集题录，不下载 PDF、不生成 LLM 摘要，因此适合订阅较多期刊并每天接收最新文章的**完整目录**。

## 一、包含文件（相对仓库根目录）

```text
.github/workflows/daily-journals.yml
config/journal_digest.yaml
scripts/journal_digest.py
state/journals-seen.json
tests/test_journal_digest.py
```

以上文件已部署到仓库的 `main` 分支。仓库内已有 `config/`；新增此文件，不要覆盖原来的 `config/base.yaml`、`config/custom.yaml` 或 `main.yml`。

## 二、设置与原有 Actions 共用

当前新工作流复用原有 Repository Secrets：

- `SENDER`：发送邮箱
- `RECEIVER`：接收邮箱
- `SENDER_PASSWORD`：SMTP 授权码

邮件服务器和端口会**优先**读取 Repository Variables `SMTP_SERVER` 和 `SMTP_PORT`；如未配置，会读取原来的 `CUSTOM_CONFIG` 中 `email.smtp_server` 和 `email.smtp_port`。例如 QQ 邮箱服务器为 `smtp.qq.com`，SSL 端口为 `465`。已配置的邮箱凭据无需重复设置，也无需填写 Zotero / OpenAI 的密钥。

如果仓库设有禁止 GitHub Actions 推送的分支保护规则，需要调整权限或使用状态存储分支。新工作流使用 `GITHUB_TOKEN` 的 `contents: write` 将已发送文章的 DOI/标题摘要保存至 `state/journals-seen.json`，防止跨日重复发送。该文件位于公开仓库时，也会公开被推送文章的标识信息，不含邮箱、授权码或文章全文。

## 三、设置期刊

编辑 `config/journal_digest.yaml` 的 `journals` 列表。默认从原来的配置移入三个 ISSN：

- `1365-1609`：*International Journal of Rock Mechanics and Mining Sciences*（已配置官方 RSS：`https://rss.sciencedirect.com/publication/science/13651609`）
- `0723-2632`：*Rock Mechanics and Rock Engineering*
- `2095-2686`：*International Journal of Mining Science and Technology*

增加期刊例如：

```yaml
  - name: Example Journal
    issn: "1234-567X"    # 须替换成准确 ISSN
    rss: ""              # 选填已核实的期刊文章 RSS/Atom URL
```

有真实 RSS 时填写 `rss`，程序会同时抓取 Crossref + RSS 并按 DOI/标题归并，RSS 没有 DOI 时只能使用标题去重；不同文章偶然重名可能误判。如果没有 Crossref ISSN，也可以只填 RSS。请不要凭空猜测 RSS URL。

注意：默认只抓取期刊目录，并**不会**因研究主题筛掉文章；如果需要 AI 精读，建议后续增加“仅对每天最高优先级 3–5 篇文章进行摘要分析”的独立可选步骤。

## 四、测试和启动

1. 提交文件后，在仓库 `Actions` 选择 **Daily journals - new articles**。
2. 点击 `Run workflow`，先保留 `dry_run=true`。它会采集论文并上传 Markdown 报告，但**不会发邮件或更新去重状态**。
3. 在运行详情的 Artifacts 中查看 `journal-digest-<run_id>`，核对文章标题、日期和 DOI。
4. 确认配置无误后再手动运行一次，选择 `dry_run=false`，检查接收邮箱是否收到期刊文章目录。
5. 之后将每天 UTC 00:37（北京时间 08:37）自动执行。需要更换时间直接修改 workflow 内的 `cron`（使用 UTC）。

无新论文时默认不发邮件；在 `config/journal_digest.yaml` 设置 `send_when_empty: true` 可以让它每天即使没有新论文也发送一份“0篇”日报。

## 五、如何定义“最新”

- Crossref 检索 **最近登记 DOI**、**最近在线发表**；已有成功抓取进度后，再按 **索引日期** 补抓新入库及更新记录。索引补抓从上次成功扫描日期前一天开始，失败多日后也不会只查询最近 7 天。
- 每篇文章优先展示日期精确到日的 `published-online`；如缺失，使用其他完整日期；最后才使用 DOI 首次登记日，并明确标注，不把登记日期当成在线发表日期。
- 日报分为 **近期在线发表（日期已核实）**、**补录 / 元数据更新（非近期在线发表）**、**新发现记录（在线发表日期待核实）**。近期窗口默认 7 个 UTC 日（含今日），仅精确到日且不晚于今天的 `published-online`，或官方 ScienceDirect RSS 明确写出的 `Publication date: Available online 日 月 年`，能进入第一组，按该日期从新到旧排序；每篇附日期依据。
- 新登记 / 新索引的记录即使发表时间较早也会列为补录，不伪装成近期论文。缺少在线日期、仅有未来卷期日期的记录列为待核实；明确的未来在线日期暂不推送，也不标记为已发送，留待日期到来后检索。RSS 的 `prism:coverDate`、`pubDate` 和 `updated` 均不能证明 Online First；同一 DOI 的官方 RSS 与 Crossref 在线日期不一致时，降为待核实并提示冲突。无 DOI 的 RSS 仅在与一篇 Crossref 记录标题完全匹配时合并日期信息。
- 日报及 Actions 摘要显示每本期刊 **本次检索可核实的最新在线发表日期**，Markdown 报告还记录抓取完成时间。这表示本次公开元数据的检查结果，不表示出版商已经完整更新，也不把抓取时间当成发表时间。
- 期刊出版商更新元数据可能有延迟、部分论文不提供可用的发表日期，程序无法保证囊括全部网站刚上架的论文。RSS 的 `updated` 也可能代表编辑更新而非首次发表，邮件会标为 RSS 发布/更新时间。
- Crossref API 错误或结果截断仍会让本次运行失败；若 RSS 获取失败、但该期刊有 ISSN，继续使用 Crossref 补检，并在日报中显式警告覆盖不完整。即使零篇也发送异常提示，且不推进成功抓取日期。正常零篇的扫描会保存抓取进度；SMTP 失败时不更新任何发送 / 抓取状态；dry run 始终不修改状态。

## 六、性能与可靠性

相比原版逐篇下载 PDF、计算与 Zotero 文献的向量距离并调用 LLM，本版本主要执行少量 Crossref API / RSS 请求，预期在多期刊情况下运行负担更小。但实际运行时间受期刊数量、Crossref 速率限制、网络及 SMTP 可用性影响，**尚未完成 GitHub Actions 云端真实测试**。

提交去重状态仅发生在 SMTP 接受邮件之后。如果邮件已发出但提交状态失败（例如仓库禁止写入），下次运行可能再发送一遍；这是一种“至少一次”的交付语义。可在 Action 的最后一步查看提交日志。

## 七、离线单元测试

```bash
python -m pip install requests PyYAML
python -m unittest discover -s tests -p 'test_journal_digest*.py' -v
```

也可在本地 `--dry-run` 执行，但必须有联网环境才能真正访问 Crossref 和 RSS：

```bash
python scripts/journal_digest.py --config config/journal_digest.yaml --state state/journals-seen.json --report-dir reports/journals --dry-run
```

新 Workflow 已部署在 GitHub 仓库；此次 IJRMMS RSS 代码修改本身不发送邮件。
