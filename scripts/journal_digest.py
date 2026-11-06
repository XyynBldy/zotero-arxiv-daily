#!/usr/bin/env python3
"""Standalone Crossref + optional RSS journal table-of-contents mailer.

Independent of zotero-arxiv-daily's embeddings, PDF extraction, and LLM stages.
Only marks papers as delivered after SMTP send succeeds. A failed git push can
cause an at-least-once duplicate on a later run; see README.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CROSSREF_API = "https://api.crossref.org/journals/{issn}/works"
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
ISSN_RE = re.compile(r"^\d{4}-\d{3}[\dXx]$")
MAX_PAGES = 100


def clean(value: object) -> str:
    if value is None:
        return ""
    value = str(value)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(value).split())


def paper_key(doi: str, title: str, journal: str) -> set[str]:
    keys: set[str] = set()
    doi = doi.strip().lower()
    if doi:
        keys.add("doi:" + doi)
    norm = re.sub(r"[^\w]+", "", title.casefold(), flags=re.UNICODE)
    if norm:
        keys.add("title:" + hashlib.sha256((journal.casefold() + ":" + norm).encode("utf-8")).hexdigest())
    return keys


def normalize_doi(value: str) -> str:
    value = clean(value).strip()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)\s*", "", value, flags=re.I)
    match = DOI_RE.search(value)
    return match.group(0).rstrip(".,;)]}").lower() if match else ""


def safe_link(url: str) -> str:
    url = str(url or "").strip()
    return url if urlsplit(url).scheme.lower() in {"http", "https"} else ""


def crossref_date(field: object) -> tuple[date | None, bool]:
    if not isinstance(field, dict):
        return None, False
    parts = (field.get("date-parts") or [[]])[0]
    if not parts:
        return None, False
    try:
        return date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1,
                    int(parts[2]) if len(parts) > 2 else 1), len(parts) == 3
    except (TypeError, ValueError, IndexError):
        return None, False


def make_crossref_paper(item: dict, journal_name: str) -> dict | None:
    doi = normalize_doi(str(item.get("DOI", "")))
    title = clean((item.get("title") or [""])[0])
    if not title or not doi:
        return None
    # 在线发表日期优先；印刷或不完整的日期不冒充首次在线发表日期。
    online, online_exact = crossref_date(item.get("published-online"))
    pub, pub_exact = crossref_date(item.get("published"))
    issued, issued_exact = crossref_date(item.get("issued"))
    created, _ = crossref_date(item.get("created"))
    if online and online_exact:
        pub_date, kind = online, "在线发表"
    elif pub and pub_exact:
        pub_date, kind = pub, "发表日期（未标明是否在线）"
    elif issued and issued_exact:
        pub_date, kind = issued, "期刊记录日期"
    else:
        pub_date, kind = created, "DOI 首次登记（发表日不详）"
    authors = [clean(" ".join(filter(None, [a.get("given", ""), a.get("family", "")])))
               for a in item.get("author", []) if isinstance(a, dict)]
    authors = [a for a in authors if a]
    return dict(title=title, doi=doi, url=f"https://doi.org/{quote(doi, safe='/')}",
                authors=authors, pub_date=pub_date, date_kind=kind,
                journal=journal_name, source="Crossref")


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, connect=3, read=3, status=3, backoff_factor=1,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "JournalTOCDigest/1.0 (public metadata; contact configured by user)",
                            "Accept": "application/json"})
    return session


def fetch_crossref(session: requests.Session, cfg: dict, issn: str, name: str,
                   first_day: date, last_day: date) -> list[dict]:
    settings = cfg.get("crossref", {})
    rows = int(settings.get("rows_per_page", 100))
    ceiling = int(settings.get("max_records_per_query", 5000))
    timeout = int(settings.get("timeout_seconds", 25))
    if rows < 1 or rows > 1000 or ceiling < 1:
        raise ValueError("Invalid Crossref paging settings")
    email = settings.get("contact_email", "")
    if email:
        session.headers["User-Agent"] = f"JournalTOCDigest/1.0 (mailto:{email})"
    matches: dict[str, dict] = {}
    # 两组查询：新登记 DOI + 近期正式在线发表。不是把 DOI 登记日误认为发表日。
    for field in ("created-date", "online-pub-date"):
        params = {
            "filter": f"type:journal-article,from-{field}:{first_day.isoformat()},until-{field}:{last_day.isoformat()}",
            "select": "DOI,title,author,created,published-online,published,issued,container-title",
            "rows": rows,
            "cursor": "*",
        }
        count = 0
        for _ in range(MAX_PAGES):
            response = session.get(CROSSREF_API.format(issn=quote(issn, safe='')),
                                   params=params, timeout=timeout)
            response.raise_for_status()
            message = response.json().get("message") or {}
            items = message.get("items") or []
            count += len(items)
            if count > ceiling:
                raise RuntimeError(f"{name}: Crossref {field} results exceed cap {ceiling}; refusing incomplete digest")
            for raw in items:
                paper = make_crossref_paper(raw, name)
                if paper:
                    matches[paper["doi"]] = paper
            if len(items) < rows:
                break
            cursor = message.get("next-cursor")
            if not cursor or cursor == params["cursor"]:
                raise RuntimeError(f"{name}: Crossref cursor missing/stalled; refusing incomplete digest")
            params["cursor"] = cursor
            time.sleep(0.15)
        else:
            raise RuntimeError(f"{name}: Crossref pagination exceeded {MAX_PAGES} pages")
    return list(matches.values())


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def first_node_text(entry: ET.Element, *names: str) -> str:
    for child in entry:
        if local_name(child.tag) in names:
            text = "".join(child.itertext()).strip()
            if text:
                return clean(text)
    return ""


def parse_rss_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def fetch_rss(session: requests.Session, url: str, name: str, timeout: int) -> list[dict]:
    if not safe_link(url):
        raise ValueError(f"{name}: invalid RSS URL (HTTP(S) required)")
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    entries = [node for node in root.iter() if local_name(node.tag) in ("item", "entry")]
    papers = []
    for entry in entries:
        title = first_node_text(entry, "title")
        if not title:
            continue
        link = ""
        for child in entry:
            if local_name(child.tag) == "link":
                link = child.attrib.get("href") or (child.text or "").strip()
                if child.attrib.get("rel") in (None, "alternate") and link:
                    break
        link = safe_link(link)
        identifier = first_node_text(entry, "doi", "id", "guid")
        doi = normalize_doi(identifier) or normalize_doi(link)
        stamp = first_node_text(entry, "published", "pubdate", "date", "updated")
        when = parse_rss_date(stamp)
        if when is None:
            # 绝不把无日期 RSS 全部判成“今天发表”。
            continue
        papers.append(dict(title=title, doi=doi,
                           url=f"https://doi.org/{quote(doi, safe='/')}" if doi else link,
                           authors=[], pub_date=when, date_kind="RSS 发布/更新日期",
                           journal=name, source="RSS"))
    return papers


def select_new(cfg: dict, state: dict, session: requests.Session,
               today: date) -> tuple[OrderedDict[str, list[dict]], set[str], dict[str, int]]:
    lookback = int(cfg.get("lookback_days", 7))
    if not 1 <= lookback <= 90:
        raise ValueError("lookback_days must be between 1 and 90")
    first_day = today - timedelta(days=lookback)
    if not cfg.get("journals"):
        raise ValueError("Configure one or more journals")
    seen = state.get("seen", {})
    additions: set[str] = set()
    result: OrderedDict[str, list[dict]] = OrderedDict()
    stats: dict[str, int] = {}
    batch_keys: set[str] = set()
    for journal in cfg["journals"]:
        name = clean(journal.get("name", ""))
        issn = str(journal.get("issn") or "").strip()
        rss = str(journal.get("rss") or "").strip()
        if not name or (not issn and not rss):
            raise ValueError(f"Incomplete journal entry: {journal!r}")
        if issn and not ISSN_RE.fullmatch(issn):
            raise ValueError(f"Invalid ISSN for {name}: {issn}")
        if name in result:
            raise ValueError(f"Duplicate journal name: {name}")
        raw: list[dict] = []
        if issn:
            raw += fetch_crossref(session, cfg, issn, name, first_day, today)
        if rss:
            raw += fetch_rss(session, rss, name, int(cfg.get("crossref", {}).get("timeout_seconds", 25)))
        eligible: list[dict] = []
        local_keys: set[str] = set()
        for paper in raw:
            pub_date = paper.get("pub_date")
            if not pub_date or not first_day <= pub_date <= today:
                continue
            keys = paper_key(paper["doi"], paper["title"], name)
            if not keys or any(key in seen or key in batch_keys or key in local_keys for key in keys):
                continue
            eligible.append(paper)
            local_keys.update(keys)
        eligible.sort(key=lambda p: (p["pub_date"], p["title"].casefold()), reverse=True)
        result[name] = eligible
        additions.update(local_keys)
        batch_keys.update(local_keys)
        stats[name] = len(raw)
    return result, additions, stats


def render_markdown(groups: OrderedDict[str, list[dict]], today: date,
                    lookback: int) -> str:
    count = sum(map(len, groups.values()))
    chunks = [f"# 指定期刊最新文章日报 · {today.isoformat()} (UTC)",
              f"检索最近 {lookback} 天，按既有推送记录去重；本期新增 **{count}** 篇。",
              "注：发表日期优先采用 Crossref 在线日期；缺失时使用其他记录日期。",
              ""]
    for name, papers in groups.items():
        chunks.append(f"## {name}（{len(papers)} 篇）")
        if not papers:
            chunks.extend(["本期无新的可确认文章。", ""])
            continue
        for idx, p in enumerate(papers, 1):
            title = p["title"].replace("[", "\\[").replace("]", "\\]")
            link = p["url"] or ""
            chunks.append(f"{idx}. **{title}**")
            chunks.append(f"   - {p['date_kind']}：{p['pub_date'].isoformat()} | DOI：{p['doi'] or '未提供'} | 来源：{p['source']}")
            if p["authors"]:
                chunks.append(f"   - 作者：{', '.join(p['authors'][:8])}" + (" 等" if len(p["authors"]) > 8 else ""))
            if link:
                chunks.append(f"   - 链接：{link}")
        chunks.append("")
    chunks.extend(["---", "检索基于 Crossref 元数据及可选 RSS；出版商登记可能延迟，不能保证覆盖所有尚未登记的论文。"])
    return "\n".join(chunks) + "\n"


def render_html(groups: OrderedDict[str, list[dict]], today: date,
                lookback: int) -> str:
    total = sum(map(len, groups.values()))
    chunks = ["<html><body style='font-family:Arial,sans-serif;line-height:1.5;max-width:900px'>",
              f"<h2>指定期刊最新文章日报 · {html.escape(today.isoformat())} (UTC)</h2>",
              f"<p>最近 {lookback} 天检索并跨天去重，本期新增 <b>{total}</b> 篇。</p>"]
    for name, papers in groups.items():
        chunks.append(f"<h3>{html.escape(name)}（{len(papers)} 篇）</h3>")
        if not papers:
            chunks.append("<p style='color:#777'>本期无新文章。</p>")
            continue
        chunks.append("<ol>")
        for p in papers:
            link = safe_link(p["url"])
            title = html.escape(p["title"])
            title_html = f'<a href="{html.escape(link, quote=True)}">{title}</a>' if link else title
            authors = ", ".join(p["authors"][:8])
            chunks.append("<li style='margin-bottom:12px'>" + f"<b>{title_html}</b>" +
                          "<div style='font-size:13px;color:#555'>" +
                          html.escape(p["date_kind"]) + ": " + p["pub_date"].isoformat() +
                          " | DOI: " + html.escape(p["doi"] or "未提供") +
                          " | " + html.escape(p["source"]) + "</div>" +
                          ("<div style='font-size:13px'>" + html.escape(authors) + "</div>" if authors else "") +
                          "</li>")
        chunks.append("</ol>")
    chunks.append("<p style='color:#777;font-size:12px'>元数据可能存在延迟；在线发表日期缺失时注明采用的替代日期。</p></body></html>")
    return "".join(chunks)


def smtp_settings() -> tuple[str, int]:
    host = os.getenv("SMTP_SERVER", "").strip()
    port = os.getenv("SMTP_PORT", "").strip()
    if not host or not port:
        custom = os.getenv("CUSTOM_CONFIG", "")
        try:
            email_cfg = (yaml.safe_load(custom) or {}).get("email", {}) if custom else {}
        except yaml.YAMLError:
            email_cfg = {}
        host = host or str(email_cfg.get("smtp_server") or "").strip()
        port = port or str(email_cfg.get("smtp_port") or "").strip()
    if not host or not port:
        raise RuntimeError("SMTP server/port missing: set vars.SMTP_SERVER & vars.SMTP_PORT, or keep email.smtp_server & smtp_port in CUSTOM_CONFIG")
    if int(port) not in (465, 587):
        raise ValueError("Only secure SMTP ports 465 (SSL) and 587 (STARTTLS) are supported")
    return host, int(port)


def send_email(subject: str, markdown: str, html_body: str) -> None:
    sender = os.getenv("SENDER", "").strip()
    receiver = os.getenv("RECEIVER", "").strip()
    password = os.getenv("SENDER_PASSWORD", "")
    if not sender or not receiver or not password:
        raise RuntimeError("Missing secrets: SENDER, RECEIVER, SENDER_PASSWORD")
    host, port = smtp_settings()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = receiver
    message.set_content(markdown)
    message.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as server:
            server.login(sender, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(sender, password)
            server.send_message(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/journal_digest.yaml")
    parser.add_argument("--state", default="state/journals-seen.json")
    parser.add_argument("--report-dir", default="reports/journals")
    parser.add_argument("--date", default="", help="UTC YYYY-MM-DD for test only")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    dry_run = args.dry_run or os.getenv("DRY_RUN", "").lower() in ("true", "1", "yes")
    today = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    state_path = Path(args.state)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"schema": 1, "seen": {}}
    if not isinstance(state.get("seen"), dict):
        raise ValueError("Invalid seen state")
    groups, keys, stats = select_new(config, state, make_session(), today)
    total = sum(len(papers) for papers in groups.values())
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report = render_markdown(groups, today, int(config.get("lookback_days", 7)))
    report_path = report_dir / f"{today.isoformat()}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"Report: {report_path} | Newly discovered: {total} | raw counts: {stats} | dry_run={dry_run}", flush=True)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fp:
            fp.write(f"### Journal digest: {today.isoformat()} UTC\n\n")
            fp.write(f"- New: **{total}**; dry run: **{dry_run}**\n")
            fp.write("\n".join(f"- {name}: {len(papers)}" for name, papers in groups.items()) + "\n")
    if dry_run:
        return 0
    if not total and not bool(config.get("send_when_empty", False)):
        print("No new papers; email skipped.")
        return 0
    subject = f"[期刊日报] {today.isoformat()} | 新文章 {total} 篇"
    send_email(subject, report, render_html(groups, today, int(config.get("lookback_days", 7))))
    # State is modified only after successful mail submission (SMTP acceptance).
    state["seen"].update({key: today.isoformat() for key in keys})
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(state_path)
    print(f"Email accepted by SMTP server; saved state: {state_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
