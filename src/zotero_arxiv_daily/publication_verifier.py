"""Fail-closed verification against the publisher article page before delivery.

Crossref and RSS identify candidates; neither alone satisfies this delivery gate.
Only explicit online-date fields on a DOI-matched publisher article page count.
"""
import hashlib
import json
import re
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests

PUBLISHER_HOSTS = frozenset({
    "www.sciencedirect.com", "sciencedirect.com", "link.springer.com", "link.springernature.com",
    "onlinelibrary.wiley.com", "agupubs.onlinelibrary.wiley.com", "ascelibrary.org",
    "pubs.geoscienceworld.org", "www.jrmge.cn", "jrmge.cn",
})
RESOLVER_HOSTS = frozenset({"doi.org", "dx.doi.org", "linkinghub.elsevier.com"})
DOI_FIELDS = frozenset({"citation_doi", "dc.identifier", "dc.identifier.doi", "prism.doi", "doi"})
ONLINE_FIELDS = frozenset({"citation_online_date", "citation_online_publication_date",
                           "available-online-date", "first-online-date"})
MONTHS = {name[:3].lower(): i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1)}
DATE_PATTERN = r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})"


def normalize_doi(value):
    value = str(value or "").strip()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)\s*", "", value, flags=re.I)
    return value.strip().lower()


def exact_date(value):
    text = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
        if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", text):
            return date(*map(int, text.split("/")))
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        match = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
        if match:
            return date(int(match[3]), MONTHS[match[2][:3].lower()], int(match[1]))
        match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text)
        if match:
            return date(int(match[3]), MONTHS[match[1][:3].lower()], int(match[2]))
    except (ValueError, KeyError):
        pass
    return None


def public_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def allowed_url(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname in PUBLISHER_HOSTS | RESOLVER_HOSTS
                and not parts.username and not parts.password and parts.port in (None, 443))
    except ValueError:
        return False


class ArticlePage(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.metadata, self.text_parts, self.refresh = [], [], None
        self.ignored = 0
        self.stack, self.history_text = [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        scope = bool(re.search(r"article[-_ ]?history|publication[-_ ]?(?:history|dates?|info)|article[-_ ]?dates?|bibliographic-information",
                               attrs.get("id", "") + " " + attrs.get("class", ""), re.I))
        if tag not in ("meta", "link", "br", "hr", "img", "input", "source", "area", "base", "wbr"):
            self.stack.append((tag, scope))
        if tag in ("script", "style", "noscript"):
            self.ignored += 1
        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            self.metadata.append((name, attrs.get("content", "")))
            if attrs.get("http-equiv", "").lower() == "refresh":
                match = re.search(r";\s*url\s*=\s*(.+)$", attrs.get("content", ""), re.I)
                if match:
                    self.refresh = match[1].strip().strip("'\"")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.ignored:
            self.ignored -= 1
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self.ignored:
            self.text_parts.append(data)
            if any(scope for _, scope in self.stack):
                self.history_text.append(data)

    def identity_and_dates(self):
        identities = {normalize_doi(value) for key, value in self.metadata if key in DOI_FIELDS}
        candidates = [(exact_date(value), key, value) for key, value in self.metadata if key in ONLINE_FIELDS]
        # Dates in references, related articles and generic page text cannot
        # establish this article's date. Require an article-history block.
        visible = " ".join(" ".join(self.history_text).split())
        for match in re.finditer(r"\b(Available\s+online|First\s+online|Published\s+online|First\s+published\s+online)\s*[:：]?\s*(" + DATE_PATTERN + r")\b", visible, re.I):
            candidates.append((exact_date(match[2]), match[1], match[2]))
        return identities, [item for item in candidates if item[0] is not None]


class PublisherDateVerifier:
    def __init__(self, session=None, timeout=10, max_checks=40):
        self.session = session or requests.Session()
        self.timeout, self.max_checks = timeout, max_checks
        self.calls, self.cache = 0, {}

    def verify(self, doi, publisher_url=None, expected_online=None, today=None):
        doi = normalize_doi(doi)
        today = today or datetime.now(timezone.utc).date()
        expected = exact_date(expected_online) if expected_online else None
        key = (doi, expected.isoformat() if expected else None, today.isoformat())
        if key in self.cache:
            return dict(self.cache[key])
        proof = {"status": "unverified", "doi": doi, "online_date": None,
                 "publisher_url": "", "checked_at": datetime.now(timezone.utc).isoformat(),
                 "crossref_online_date": expected.isoformat() if expected else None, "attempted": False}
        if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            proof["reason"] = "缺少可核对的 DOI"
            return proof
        if self.calls >= self.max_checks:
            proof["reason"] = "本次核查额度已用尽，留待下次复核"
            return proof
        self.calls += 1
        proof["attempted"] = True
        url = publisher_url or "https://doi.org/" + quote(doi, safe="/")
        try:
            for _ in range(6):
                if not allowed_url(url):
                    proof["reason"] = "未配置或不安全的出版商地址"
                    break
                proof["publisher_url"] = public_url(url)
                response = self.session.get(url, timeout=(5, self.timeout), allow_redirects=False,
                                            stream=True, headers={"Cache-Control": "no-cache",
                                            "User-Agent": "JournalPublicationDateVerifier/1.0"})
                try:
                    proof["http_status"] = response.status_code
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = response.headers.get("Location")
                        if not target:
                            proof["reason"] = "官网重定向缺少地址"
                            break
                        url = urljoin(url, target)
                        continue
                    if response.status_code != 200:
                        proof["reason"] = f"官网返回 HTTP {response.status_code}，未核实"
                        break
                    chunks, length = [], 0
                    for chunk in response.iter_content(65536):
                        length += len(chunk)
                        if length > 2_000_000:
                            raise ValueError("官网页面超过核查大小限制")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    page = ArticlePage(raw.decode(response.encoding or "utf-8", errors="replace"))
                finally:
                    response.close()
                host = urlsplit(url).hostname
                if host in RESOLVER_HOSTS:
                    if page.refresh:
                        url = urljoin(url, page.refresh)
                        continue
                    proof["reason"] = "DOI 解析页未到达出版商文章页"
                    break
                identities, candidates = page.identity_and_dates()
                if identities != {doi}:
                    proof["reason"] = "官网页面 DOI 未能与目标文章唯一匹配"
                    break
                unique = {item[0] for item in candidates}
                if len(unique) != 1:
                    proof["reason"] = "官网未明确提供唯一且精确到日的在线发表日期"
                    proof["status"] = "conflict" if len(unique) > 1 else "unverified"
                    break
                online = next(iter(unique))
                proof.update(online_date=online.isoformat(), date_label=candidates[0][1],
                             date_text=candidates[0][2], evidence_sha256=hashlib.sha256(raw).hexdigest())
                if online > today:
                    proof.update(status="future", reason="官网在线日期晚于当前检索日期")
                elif expected is not None and online != expected:
                    proof.update(status="conflict", reason="官网在线日期与 Crossref / 官方 RSS 不一致")
                else:
                    proof.update(status="verified", reason="官网文章 DOI 与明确在线日期已核实")
                break
            else:
                proof["reason"] = "官网重定向次数超过限制"
        except (requests.RequestException, ValueError, UnicodeError) as exc:
            proof["reason"] = f"官网核查失败（{type(exc).__name__}）"
        self.cache[key] = dict(proof)
        return proof


def assert_verified(proof, doi, online_date, today=None):
    today = today or datetime.now(timezone.utc).date()
    online = exact_date(online_date)
    try:
        checked = datetime.fromisoformat(proof.get("checked_at", "").replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        valid = (proof.get("status") == "verified" and normalize_doi(proof.get("doi")) == normalize_doi(doi)
                 and bool(re.fullmatch(r"10\.\d{4,9}/\S+", normalize_doi(doi)))
                 and online is not None and online <= today and proof.get("online_date") == online.isoformat()
                 and urlsplit(proof.get("publisher_url", "")).hostname in PUBLISHER_HOSTS
                 and allowed_url(proof.get("publisher_url", "")) and proof.get("http_status") == 200
                 and bool(proof.get("date_label")) and bool(re.fullmatch(r"[0-9a-f]{64}", proof.get("evidence_sha256", "")))
                 and 0 <= age <= 3600)
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"在线日期未通过推送前官网核实：{doi}")


def json_state(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                      default=lambda obj: obj.isoformat() if isinstance(obj, (date, datetime)) else str(obj)) + "\n"


def restore_dates(paper):
    result = dict(paper)
    for field in ("online_date", "pub_date", "created_date", "indexed_date", "rss_date", "first_seen"):
        if isinstance(result.get(field), str):
            result[field] = exact_date(result[field])
    return result
