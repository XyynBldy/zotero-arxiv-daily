# Zotero-arxiv-daily：独立的指定期刊每日新论文邮件

本仓库提供独立的**期刊完整目录日报**，仅采集题录，不下载 PDF、不生成 LLM 摘要。它与按 Zotero 兴趣排序的期刊推荐邮件并行运行。

原有推荐邮件也限定为期刊：`Send emails daily` / `Test` 强制 `executor.journals_only=true`，订阅范围来自 `CUSTOM_CONFIG` 中的 `source.journals.issns`（未覆盖时用 `config/base.yaml` 默认值）。旧配置中的 arXiv 来源不再执行。两条流程都必须通过推送前的官网日期核查；邮件显示在线日期、依据、核查时间及补录状态，未核实记录不进入邮件。AI 连接失败时标为“原文摘要（AI 生成失败）”。两条流程使用各自的订阅列表与去重状态。

推荐邮件状态保存在 `state/recommendation-journals.json`，目录日报状态在 `state/journals-seen.json`。已发送 DOI 仅在 SMTP 接受后更新；待核查 / 待发送条目和核查凭据提前保存，发送失败也不会丢失队列。正常完成的元数据扫描可推进抓取进度，待核查记录独立复查，不受 7 天检索窗口限制。debug / dry run 不写持久状态。工作流即使发送步骤失败，也会尝试提交安全保存的队列和核查记录；提交失败仍需检查 Actions 最后一步。

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
- **官网日期核查是发送的硬性条件。** 出版商 HTTPS 文章页必须有与目标唯一匹配的 DOI，并在明确的在线日期元数据字段（如 `citation_online_date`）或文章历史区域中给出 `Available online` / `First online` / `Published online` 等精确到日的日期。泛用 `datePublished`、卷期日期、接收日期、登记时间和相关文献的日期均不能替代。
- 官网日期晚于当前检索日期、与 Crossref / 官方 RSS 不一致、缺失或不唯一时，均暂缓。HTTP 403 / 429、超时、不支持的出版商及达到核查额度也暂缓，不回退成“仅元数据通过”。不绕过出版商访问限制。每次推送重新获取凭据，发送前再检查 DOI、日期、官网域名和一小时内的核查时间。
- 近期窗口默认 7 个 UTC 日（含今日）；官网日期位于窗口内的进入 **近期在线发表（日期已核实）**，按日期从新到旧排序。已核实但日期更早的新发现记录单列为 **补录 / 元数据更新**。**未核实记录不再出现在邮件中**，仅保存在待核查队列和诊断记录里。
- 两条流程分别保存 `pending_date_checks` 和 `date_verification_audit`。凭据包含 DOI、官网地址、字段标签、原始日期文本、检查时间、HTTP 状态和页面 SHA-256；只对核实成功的页面保存哈希，不保存页面全文或访问凭据。目录日报同时上传 `YYYY-MM-DD-date-verification.json`。队列只复查仍在订阅范围内的记录。
- 为限制运行时长，目录日报每本期刊每次最多进行 8 次官网核查，推荐流程每次最多 40 次；按上次实际核查时间轮转，其余继续排队。未知日期条目不会被标记为已发送，也不会因为滑动时间窗结束而丢失。
- 日报及 Actions 摘要中的“最新在线日期”只来自本次官网核查成功的记录。若没有通过核查的日期，则明确说明；这不保证出版商已经公开所有新论文。
- Crossref 错误或结果截断仍会让运行失败；RSS 失败时可用 Crossref 发现候选，但候选仍须独立通过官网核查。SMTP 失败不推进发送 / 抓取状态，已保存的待核查记录继续保留；dry run 不修改持久状态。

## 六、性能与可靠性

目录日报不下载 PDF、不调用 LLM；逐篇官网核查会增加 HTTP 请求。实际运行时间受期刊数量、出版商访问限制、Crossref 速率限制、网络及 SMTP 可用性影响。若官网无法自动读取，推送数量可能降为零；此时保留核查失败原因和待复查记录。

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
