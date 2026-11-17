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

# Keep the standalone workflow independent of the heavy application dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zotero_arxiv_daily.journal_dates import (  # noqa: E402
    FRESHNESS_LABELS, classify_freshness, crossref_date, crossref_dates,
)
from zotero_arxiv_daily.publication_verifier import (  # noqa: E402
    PublisherDateVerifier, assert_verified, json_state, restore_dates,
)

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


def make_crossref_paper(item: dict, journal_name: str) -> dict | None:
    doi = normalize_doi(str(item.get("DOI", "")))
    title = clean((item.get("title") or [""])[0])
    if not title or not doi:
        return None
    dates = crossref_dates(item)
    authors = [clean(" ".join(filter(None, [a.get("given", ""), a.get("family", "")])))
               for a in item.get("author", []) if isinstance(a, dict)]
    authors = [a for a in authors if a]
    return dict(title=title, doi=doi, url=f"https://doi.org/{quote(doi, safe='/')}",
                authors=authors, **dates,
                journal=journal_name, source="Crossref",
                publisher_url=(item.get("resource") or {}).get("primary", {}).get("URL"))


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, connect=3, read=3, status=3, backoff_factor=1,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "JournalTOCDigest/1.0 (public metadata; contact configured by user)",
                            "Accept": "application/json", "Cache-Control": "no-cache"})
    return session


def fetch_crossref(session: requests.Session, cfg: dict, issn: str, name: str,
                   first_day: date, last_day: date, indexed_since: date | None = None) -> list[dict]:
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
    queries = [("created-date", first_day), ("online-pub-date", first_day)]
    if indexed_since is not None:
        queries.append(("index-date", indexed_since))
    for field, lower in queries:
        params = {
            "filter": f"type:journal-article,from-{field}:{lower.isoformat()},until-{field}:{last_day.isoformat()}",
            "select": "DOI,title,author,created,indexed,published-online,published,issued,container-title,resource",
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
                    paper["matched_recent_metadata"] = (
                        field in ("created-date", "index-date") or
                        matches.get(paper["doi"], {}).get("matched_recent_metadata", False)
                    )
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
    # Preserve field priority. Publishers use several namespace versions.
    for name in names:
        for child in entry:
            if local_name(child.tag) == name:
                value = "".join(child.itertext()).strip()
                if value:
                    return clean(value)
    return ""


def parse_rss_date(value: str) -> date | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.date()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(value[:10])
        except (TypeError, ValueError):
            return None


def sciencedirect_online_date(entry: ET.Element, feed_url: str) -> date | None:
    """Accept an explicit Available online date, never an issue month/year."""
    if urlsplit(feed_url).hostname != "rss.sciencedirect.com":
        return None
    description = first_node_text(entry, "description", "summary")
    match = re.match(r"^Publication date:\s*Available online\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})(?:\s|$)",
                     description, flags=re.I)
    if not match:
        return None
    months = {name.lower(): i for i, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July", "August",
         "September", "October", "November", "December"), 1)}
    try:
        return date(int(match[3]), months[match[2].lower()], int(match[1]))
    except (KeyError, ValueError):
        return None


def fetch_rss(session: requests.Session, url: str, name: str, timeout: int) -> list[dict]:
    if not safe_link(url):
        raise ValueError(f"{name}: invalid RSS URL (HTTP(S) required)")
    response = session.get(
        url, timeout=timeout,
        headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                 "Cache-Control": "no-cache"},
    )
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
        identifier = first_node_text(entry, "doi", "identifier", "id", "guid")
        doi = normalize_doi(identifier) or normalize_doi(link)
        # PRISM coverDate is the issue's cover date, NOT Online First.
        # RSS pubDate / Atom updated can indicate feed updates, not first publication.
        candidates = (
            ("publicationdate", "RSS PRISM 发表日期（未核实 Online First）"),
            ("published", "Atom 发表日期（未核实 Online First）"),
            ("pubdate", "RSS 条目发布日期（非 Online First 证明）"),
            ("date", "RSS DC 日期（可能为更新日期）"),
            ("updated", "Atom 更新时间（非发表日期）"),
        )
        when = None
        kind = ""
        for field, label in candidates:
            when = parse_rss_date(first_node_text(entry, field))
            if when is not None:
                kind = label
                break
        online = sciencedirect_online_date(entry, url)
        if online is not None:
            when, kind = online, "在线发表（ScienceDirect 官方 RSS Available online）"
        if when is None:
            # Never infer Online First from an issue's cover date or today's fetch.
            continue
        creator = first_node_text(entry, "creator", "author")
        papers.append(dict(title=title, doi=doi,
                           url=f"https://doi.org/{quote(doi, safe='/')}" if doi else link,
                           authors=[creator] if creator else [], pub_date=when, date_kind=kind,
                           rss_date=when, online_date=online,
                           online_date_source="ScienceDirect 官方 RSS Available online" if online else "",
                           journal=name, source="RSS"))
    return papers

def select_new(cfg: dict, state: dict, session: requests.Session,
               today: date, warnings: list[str] | None = None,
               source_status: dict | None = None) -> tuple[OrderedDict[str, list[dict]], set[str], dict[str, int]]:
    lookback = int(cfg.get("lookback_days", 7))
    if not 1 <= lookback <= 90:
        raise ValueError("lookback_days must be between 1 and 90")
    first_day = today - timedelta(days=lookback - 1)
    indexed_since = None
    if state.get("last_successful_fetch"):
        last_fetch = date.fromisoformat(state["last_successful_fetch"])
        if last_fetch > today:
            raise ValueError("last_successful_fetch is in the future")
        # Overlap one UTC day; only advance after every source and delivery succeed.
        indexed_since = last_fetch - timedelta(days=1)
    if not cfg.get("journals"):
        raise ValueError("Configure one or more journals")
    seen = state.get("seen", {})
    old_pending = state.get("pending_date_checks", {})
    pending = {}
    audit = state.setdefault("date_verification_audit", {})
    additions: set[str] = set()
    result: OrderedDict[str, list[dict]] = OrderedDict()
    stats: dict[str, int] = {}
    batch_keys: set[str] = set()
    for journal in cfg["journals"]:
        verifier = PublisherDateVerifier(max_checks=8)
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
            raw += fetch_crossref(session, cfg, issn, name, first_day, today, indexed_since)
        if rss:
            try:
                raw += fetch_rss(session, rss, name, int(cfg.get("crossref", {}).get("timeout_seconds", 25)))
            except (requests.RequestException, ET.ParseError, ValueError) as exc:
                if not issn:
                    raise RuntimeError(f"{name}: RSS failed and no Crossref backup") from exc
                note = f"{name}：RSS 抓取失败（{type(exc).__name__}），本次仅使用 Crossref 补检；RSS 文章覆盖可能不完整。"
                print("WARNING: " + note, file=sys.stderr, flush=True)
                if warnings is not None:
                    warnings.append(note)
        fresh_keys = {p["doi"] or p["title"].casefold() for p in raw}
        for item in old_pending.values():
            if (item.get("journal") == name and item.get("subscription_issn") == issn and
                    (item["doi"] or item["title"].casefold()) not in fresh_keys):
                raw.append(dict(restore_dates(item), matched_recent_metadata=True))
        # Crossref has the authoritative online date; an RSS update for the same
        # DOI must not replace it or make an old paper appear newly published.
        by_doi = {p["doi"]: p for p in raw if p["doi"] and p["source"] == "Crossref"}
        by_title: dict[str, list[dict]] = {}
        for paper in by_doi.values():
            by_title.setdefault(clean(paper["title"]).casefold(), []).append(paper)
        merged_rss: set[int] = set()
        for paper in raw:
            if paper["source"] != "RSS":
                continue
            target = by_doi.get(paper["doi"])
            if target is None and not paper["doi"]:
                title_matches = by_title.get(clean(paper["title"]).casefold(), [])
                if len(title_matches) == 1:
                    target = title_matches[0]
            if target is None:
                continue
            target["rss_date"] = paper.get("rss_date", paper.get("pub_date"))
            if paper.get("online_date"):
                if target.get("online_date") and target["online_date"] != paper["online_date"]:
                    if warnings is not None:
                        warnings.append(f"{name}：存在来源日期冲突，相关记录已暂缓推送，详见核查报告。")
                    target["date_conflict"] = True
                    target["online_date"] = None
                    target["date_kind"] = "来源日期冲突（需人工核实）"
                else:
                    target["online_date"] = paper["online_date"]
                    target["pub_date"] = paper["online_date"]
                    target["date_kind"] = paper["date_kind"]
                    target["online_date_source"] = paper.get("online_date_source", "官方 RSS 明确在线日期（尚待官网复核）")
            merged_rss.add(id(paper))
        raw = [p for p in raw if id(p) not in merged_rss]
        verified_dates = []
        eligible: list[dict] = []
        local_keys: set[str] = set()
        def last_attempt(paper):
            key = paper["doi"] or "title:" + hashlib.sha256((name + paper["title"]).encode()).hexdigest()
            return audit.get(key, {}).get("last_attempted_at", "")
        for paper in sorted(raw, key=last_attempt):
            queue_key = paper["doi"] or "title:" + hashlib.sha256((name + paper["title"]).encode()).hexdigest()
            paper["subscription_issn"] = issn
            paper.setdefault("first_seen", today)
            proof = verifier.verify(paper["doi"], paper.get("publisher_url") or paper.get("url"),
                                    paper.get("online_date"), today)
            if paper.get("date_conflict"):
                proof = dict(proof, status="conflict", reason="Crossref 与官方 RSS 日期存在冲突")
            proof["last_attempted_at"] = (proof["checked_at"] if proof.get("attempted") else
                                           audit.get(queue_key, {}).get("last_attempted_at", ""))
            audit[queue_key] = proof
            try:
                assert_verified(proof, paper["doi"], proof.get("online_date"), today)
            except ValueError:
                pending[queue_key] = paper
                continue
            paper["date_verification"] = proof
            paper["online_date"] = date.fromisoformat(proof["online_date"])
            paper["pub_date"] = paper["online_date"]
            paper["date_kind"] = "官网核实的在线发表日期"
            paper["online_date_source"] = proof["publisher_url"] + " · " + proof["date_label"]
            verified_dates.append(paper["online_date"])
            freshness = classify_freshness(paper, first_day, today, indexed_since)
            if freshness is None:
                continue
            paper["freshness"] = freshness
            keys = {"doi:" + paper["doi"]}  # Strict DOI identity; homonymous papers remain distinct.
            doi_key = "doi:" + paper["doi"]
            # A previously unverified / advance issue record may become Online
            # First after it was sent. Announce that new publication date once.
            newly_online = (freshness == "recent_online" and doi_key in seen and
                            paper["online_date"] > date.fromisoformat(seen[doi_key]))
            if (not keys or any(key in batch_keys or key in local_keys for key in keys) or
                    (not newly_online and any(key in seen for key in keys))):
                continue
            eligible.append(paper)
            pending[queue_key] = paper
            local_keys.update(keys)
        def sort_date(paper):
            if paper.get("online_date"):
                return paper["online_date"]
            return max((value for value in (paper.get("created_date"), paper.get("indexed_date"),
                        paper.get("rss_date"), paper.get("pub_date")) if value and value <= today),
                       default=date.min)
        eligible.sort(key=lambda p: (sort_date(p), p["title"].casefold()), reverse=True)
        result[name] = eligible
        additions.update(local_keys)
        batch_keys.update(local_keys)
        stats[name] = len(raw)
        if source_status is not None:
            source_status[name] = max(verified_dates).isoformat() if verified_dates else "本次没有通过官网核查的在线日期"
    state["pending_date_checks"] = pending
    return result, additions, stats


def render_markdown(groups: OrderedDict[str, list[dict]], today: date,
                    lookback: int, warnings: list[str] | None = None,
                    source_status: dict | None = None) -> str:
    count = sum(map(len, groups.values()))
    chunks = [f"# 指定期刊最新文章日报 · {today.isoformat()} (UTC)",
              f"近期在线发表窗口：最近 {lookback} 个 UTC 日（含今日）；本期通过官网日期核实 **{count}** 篇。",
              "仅推送已在出版商文章页核对 DOI 和明确在线日期的记录；未核实及日期冲突的记录留待复查。",
              ""]
    if warnings:
        chunks.extend(["## 数据源提示", *[f"- {warning}" for warning in warnings], ""])
    for name, papers in groups.items():
        chunks.append(f"## {name}（{len(papers)} 篇）")
        if source_status is not None:
            chunks.append(f"本次检索可核实的最新在线发表日期：{source_status.get(name, '未取得')}（非出版商更新完成的保证）")
        if not papers:
            chunks.extend(["本期无新的可确认文章。", ""])
            continue
        for category, section in publication_sections(papers):
            chunks.append(f"### {FRESHNESS_LABELS[category]}（{len(section)} 篇）")
            for idx, p in enumerate(section, 1):
                title = p["title"].replace("[", "\\[").replace("]", "\\]")
                link = p["url"] or ""
                chunks.append(f"{idx}. **{title}**")
                chunks.append(f"   - {date_description(p, today)} | DOI：{p['doi'] or '未提供'} | 来源：{p['source']}")
                chunks.append(f"   - 本监测系统首次发现：{p.get('first_seen', today).isoformat()}（非在线发表日期）")
                if p.get("created_date"):
                    chunks.append(f"   - DOI 登记日期：{p['created_date'].isoformat()}（非在线发表日期）")
                if p["authors"]:
                    chunks.append(f"   - 作者：{', '.join(p['authors'][:8])}" + (" 等" if len(p["authors"]) > 8 else ""))
                if link:
                    chunks.append(f"   - 链接：{link}")
        chunks.append("")
    chunks.extend(["---", "检索基于 Crossref 元数据及可选 RSS；出版商登记可能延迟，不能保证覆盖所有尚未登记的论文。"])
    return "\n".join(chunks) + "\n"


def publication_sections(papers: list[dict]):
    for category in FRESHNESS_LABELS:
        section = [p for p in papers if p.get("freshness", "unverified") == category]
        if section:
            yield category, section


def date_description(paper: dict, today: date) -> str:
    if paper.get("online_date"):
        proof = paper.get("date_verification") or {}
        return ("在线发表：" + paper["online_date"].isoformat() + "；日期依据：" +
                paper.get("online_date_source", "来源未提供") + "；官网核查时间：" + proof.get("checked_at", "未记录"))
    value = paper.get("pub_date")
    detail = f"{paper['date_kind']}：{value.isoformat()}" if value else "无可用的精确发表日期"
    if value and value > today:
        detail += "（未来日期，不作为近期发表依据）"
    return "在线发表日期待核实；" + detail


def render_html(groups: OrderedDict[str, list[dict]], today: date,
                lookback: int, warnings: list[str] | None = None,
                source_status: dict | None = None) -> str:
    total = sum(map(len, groups.values()))
    chunks = ["<html><body style='font-family:Arial,sans-serif;line-height:1.5;max-width:900px'>",
              f"<h2>指定期刊最新文章日报 · {html.escape(today.isoformat())} (UTC)</h2>",
              f"<p>近期在线发表窗口：最近 {lookback} 个 UTC 日（含今日）；通过官网日期核实 <b>{total}</b> 篇。</p>",
              "<p>仅推送官网文章 DOI 与明确在线日期均核实通过的记录；其余暂缓并留待复查。</p>"]
    if warnings:
        chunks.append("<h3>数据源提示</h3><ul>")
        chunks.extend(f"<li>{html.escape(warning)}</li>" for warning in warnings)
        chunks.append("</ul>")
    for name, papers in groups.items():
        chunks.append(f"<h3>{html.escape(name)}（{len(papers)} 篇）</h3>")
        if source_status is not None:
            chunks.append("<p>本次检索可核实的最新在线发表日期：" +
                          html.escape(source_status.get(name, "未取得")) + "（非出版商更新完成的保证）</p>")
        if not papers:
            chunks.append("<p style='color:#777'>本期无新文章。</p>")
            continue
        for category, section in publication_sections(papers):
            chunks.append(f"<h4>{FRESHNESS_LABELS[category]}（{len(section)} 篇）</h4><ol>")
            for p in section:
                link = safe_link(p["url"])
                title = html.escape(p["title"])
                title_html = f'<a href="{html.escape(link, quote=True)}">{title}</a>' if link else title
                authors = ", ".join(p["authors"][:8])
                chunks.append("<li style='margin-bottom:12px'>" + f"<b>{title_html}</b>" +
                              "<div style='font-size:13px;color:#555'>" + html.escape(date_description(p, today)) +
                              " | DOI: " + html.escape(p["doi"] or "未提供") +
                              " | " + html.escape(p["source"]) + "</div>" +
                              "<div style='font-size:12px;color:#777'>本系统首次发现：" +
                              p.get("first_seen", today).isoformat() + "（不是在线发表日期）</div>" +
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


def save_state(state_path, state):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json_state(state), encoding="utf-8")
    temp.replace(state_path)


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
    warnings: list[str] = []
    source_status: dict = {}
    groups, keys, stats = select_new(config, state, make_session(), today, warnings, source_status)
    total = sum(len(papers) for papers in groups.values())
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report = render_markdown(groups, today, int(config.get("lookback_days", 7)), warnings, source_status)
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report += f"\n抓取完成时间：{checked_at}；日期核实以本次接口返回的元数据为准。\n"
    report_path = report_dir / f"{today.isoformat()}.md"
    report_path.write_text(report, encoding="utf-8")
    (report_dir / f"{today.isoformat()}-date-verification.json").write_text(
        json_state(state.get("date_verification_audit", {})), encoding="utf-8")
    held = sum(state.get("date_verification_audit", {}).get(key, {}).get("status") != "verified"
               for key in state.get("pending_date_checks", {}))
    print(f"Report: {report_path} | Newly discovered: {total} | raw counts: {stats} | dry_run={dry_run}", flush=True)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fp:
            fp.write(f"### Journal digest: {today.isoformat()} UTC\n\n")
            fp.write(f"- New: **{total}**; dry run: **{dry_run}**\n")
            fp.write(f"- Date verification pending/failed: **{held}**; unverified articles excluded from email.\n")
            fp.write("\n".join(f"- {name}: {len(papers)}" for name, papers in groups.items()) + "\n")
            fp.write("\n".join(f"- {name} 本次可核实最新在线日期: {value}" for name, value in source_status.items()) + "\n")
            if warnings:
                fp.write("\n".join(f"- ⚠️ {warning}" for warning in warnings) + "\n")
    if dry_run:
        return 0
    # Persist pending checks before SMTP without advancing delivery / scan state.
    # Even if SMTP fails, the next run can retry all held and unsent articles.
    save_state(state_path, state)
    if not total and not warnings and not bool(config.get("send_when_empty", False)):
        print("No new papers; email skipped.")
    else:
        for papers in groups.values():
            for paper in papers:
                assert_verified(paper.get("date_verification"), paper["doi"], paper.get("online_date"), today)
        recent = sum(p.get("freshness") == "recent_online" for papers in groups.values() for p in papers)
        subject = f"[期刊日报] {today.isoformat()} | 近期在线 {recent} 篇 / 新发现及补录 {total} 篇"
        if warnings:
            subject += " | 数据源异常"
        send_email(subject, report, render_html(groups, today, int(config.get("lookback_days", 7)), warnings, source_status))
    # Never advance on source degradation or failed SMTP; empty successful scans
    # still persist their progress so the next index query has a known boundary.
    state["seen"].update({key: today.isoformat() for key in keys})
    for papers in groups.values():
        for paper in papers:
            state.get("pending_date_checks", {}).pop(paper["doi"], None)
    if not warnings:
        state["last_successful_fetch"] = today.isoformat()
    save_state(state_path, state)
    print(f"Saved delivery / fetch state: {state_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
