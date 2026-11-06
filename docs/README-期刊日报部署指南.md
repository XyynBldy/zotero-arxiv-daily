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

按相同目录结构复制到现有 GitHub 仓库，提交到默认分支 `main`。仓库内已有 `config/`；新增此文件，不要覆盖原来的 `config/base.yaml`、`config/custom.yaml` 或 `main.yml`。

## 二、设置与原有 Actions 共用

当前新工作流复用原有 Repository Secrets：

- `SENDER`：发送邮箱
- `RECEIVER`：接收邮箱
- `SENDER_PASSWORD`：SMTP 授权码

邮件服务器和端口会**优先**读取 Repository Variables `SMTP_SERVER` 和 `SMTP_PORT`；如未配置，会读取原来的 `CUSTOM_CONFIG` 中 `email.smtp_server` 和 `email.smtp_port`。例如 QQ 邮箱服务器为 `smtp.qq.com`，SSL 端口为 `465`。已配置的邮箱凭据无需重复设置，也无需填写 Zotero / OpenAI 的密钥。

如果仓库设有禁止 GitHub Actions 推送的分支保护规则，需要调整权限或使用状态存储分支。新工作流使用 `GITHUB_TOKEN` 的 `contents: write` 将已发送文章的 DOI/标题摘要保存至 `state/journals-seen.json`，防止跨日重复发送。该文件位于公开仓库时，也会公开被推送文章的标识信息，不含邮箱、授权码或文章全文。

## 三、设置期刊

编辑 `config/journal_digest.yaml` 的 `journals` 列表。默认从原来的配置移入三个 ISSN：

- `1365-1609`：*International Journal of Rock Mechanics and Mining Sciences*
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

- Crossref 检索两个入口：**最近登记 DOI** 和 **最近在线发表**。
- 每篇文章优先展示日期精确到日的 `published-online`；如缺失，使用其他完整日期；最后才使用 DOI 首次登记日，并明确标注，不把登记日期当成在线发表日期。
- 只保留日期处于滑动窗口（默认 7 天）的文章，跨天记录已成功发送的 DOI 与标题摘要。初次部署可能一次发送过去 7 天的文章。
- 期刊出版商更新元数据可能有延迟、部分论文不提供可用的发表日期，程序无法保证囊括全部网站刚上架的论文。RSS 的 `updated` 也可能代表编辑更新而非首次发表，邮件会标为 RSS 发布/更新时间。
- 现有实现对某个期刊的 API 错误或结果截断会让本次运行失败，以防误把不完整目录写成“0 篇”。

## 六、性能与可靠性

相比原版逐篇下载 PDF、计算与 Zotero 文献的向量距离并调用 LLM，本版本主要执行少量 Crossref API / RSS 请求，预期在多期刊情况下运行负担更小。但实际运行时间受期刊数量、Crossref 速率限制、网络及 SMTP 可用性影响，**尚未完成 GitHub Actions 云端真实测试**。

提交去重状态仅发生在 SMTP 接受邮件之后。如果邮件已发出但提交状态失败（例如仓库禁止写入），下次运行可能再发送一遍；这是一种“至少一次”的交付语义。可在 Action 的最后一步查看提交日志。

## 七、离线单元测试

```bash
python -m pip install requests PyYAML
python -m unittest discover -s tests -v
```

也可在本地 `--dry-run` 执行，但必须有联网环境才能真正访问 Crossref 和 RSS：

```bash
python scripts/journal_digest.py --config config/journal_digest.yaml --state state/journals-seen.json --report-dir reports/journals --dry-run
```

该补丁只用于新 Workflow；**没有修改线上仓库的任何文件，也没有实际向你的邮箱发送邮件**。
