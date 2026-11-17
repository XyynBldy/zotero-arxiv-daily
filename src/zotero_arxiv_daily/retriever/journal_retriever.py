import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from loguru import logger

from .base import register_retriever
from .crossref_base import CrossrefBase
from ..journal_dates import classify_freshness, crossref_dates
from ..protocol import Paper
from ..publication_verifier import PublisherDateVerifier, assert_verified, json_state


@register_retriever("journals")
class JournalRetriever(CrossrefBase):
    """Subscribed ISSNs only; distinguish Online First from metadata arrival."""

    api_url = "https://api.crossref.org/works"

    def _retrieve_raw_papers(self):
        days = self.retriever_config.lookback_days
        issns = self.retriever_config.issns
        if not issns or not isinstance(days, int) or not 1 <= days <= 90:
            raise ValueError("Configure source.journals.issns and lookback_days between 1 and 90.")
        self._scan_date = datetime.now(timezone.utc).date()
        start, end = self._scan_date - timedelta(days=days), self._scan_date
        self._state_path = Path(self.retriever_config.get("state_path", "state/recommendation-journals.json"))
        self._state = (json.loads(self._state_path.read_text(encoding="utf-8"))
                       if self._state_path.exists() else {"schema": 1, "seen": {}})
        if not isinstance(self._state.get("seen"), dict):
            raise ValueError("Invalid journal recommendation state")
        indexed_since = None
        if self._state.get("last_successful_fetch"):
            last_fetch = datetime.strptime(self._state["last_successful_fetch"], "%Y-%m-%d").date()
            if last_fetch > end:
                raise ValueError("Journal fetch progress is in the future")
            indexed_since = last_fetch - timedelta(days=1)
        queries = [("created-date", start), ("online-pub-date", start)]
        if indexed_since is not None:
            queries.append(("index-date", indexed_since))
        subscribed = set(map(str, issns))
        records = {doi: dict(item, _recent_metadata=True)
                   for doi, item in self._state.get("pending_date_checks", {}).items()
                   if item.get("_subscribed_issn") in subscribed}
        verifier = PublisherDateVerifier()
        for issn in issns:
            for field, lower in queries:
                params = {
                    "filter": f"type:journal-article,issn:{issn},from-{field}:{lower},until-{field}:{end}",
                    "select": "DOI,title,abstract,author,container-title,created,indexed,published-online,published,issued,resource",
                    "rows": 100, "cursor": "*",
                }
                count = 0
                for _ in range(100):
                    message = self._get_json(params)["message"]
                    items = message["items"]
                    count += len(items)
                    if count > 5000:
                        raise RuntimeError(f"Crossref {issn}: too many records; refusing incomplete results")
                    for item in items:
                        doi = item["DOI"].strip().lower()
                        if not doi:
                            continue
                        item = dict(item)
                        item["_subscribed_issn"] = str(issn)
                        item["_recent_metadata"] = (field != "online-pub-date" or
                                                    records.get(doi, {}).get("_recent_metadata", False))
                        records[doi] = item
                    if len(items) < params["rows"]:
                        break
                    cursor = message.get("next-cursor")
                    if not cursor or cursor == params["cursor"]:
                        raise RuntimeError(f"Crossref {issn}: cursor missing/stalled")
                    params["cursor"] = cursor
                else:
                    raise RuntimeError(f"Crossref {issn}: pagination exceeded 100 pages")
                logger.info(f"Crossref {issn} | {field} | {lower} to {end} UTC | {count} records")
        result, pending = [], {}
        audit = self._state.setdefault("date_verification_audit", {})
        for doi, item in sorted(records.items(), key=lambda pair: audit.get(pair[0], {}).get("last_attempted_at", "")):
            dates = crossref_dates(item)
            publisher_url = (item.get("resource") or {}).get("primary", {}).get("URL")
            proof = verifier.verify(doi, publisher_url, dates["online_date"], end)
            proof["last_attempted_at"] = (proof["checked_at"] if proof.get("attempted") else
                                         audit.get(doi, {}).get("last_attempted_at", ""))
            audit[doi] = proof
            try:
                assert_verified(proof, doi, proof.get("online_date"), end)
            except ValueError:
                pending[doi] = item
                continue
            dates["online_date"] = datetime.strptime(proof["online_date"], "%Y-%m-%d").date()
            dates["matched_recent_metadata"] = item["_recent_metadata"]
            freshness = classify_freshness(dates, start, end, indexed_since)
            if freshness is None:
                continue
            sent = self._state["seen"].get(doi)
            newly_online = (freshness == "recent_online" and sent and
                            dates["online_date"].isoformat() > sent)
            if sent and not newly_online:
                continue
            item["_freshness"] = freshness
            item["_date_verification"] = proof
            pending[doi] = item  # Retain until SMTP acceptance, including items beyond top K.
            result.append(item)
        self._state["pending_date_checks"] = pending
        held = sum(audit.get(doi, {}).get("status") != "verified" for doi in pending)
        logger.info(f"Publisher date verification: {len(result)} eligible; {held} held for recheck")
        self._save_state()
        return result[:10] if self.config.executor.debug else result

    def convert_to_paper(self, item):
        title = self._clean_text(next(iter(item.get("title") or []), ""))
        if not title:
            return None
        authors = [self._author_name(a) for a in item.get("author", [])]
        affiliations = list(dict.fromkeys(
            self._clean_text(affiliation["name"])
            for author in item.get("author", []) for affiliation in (author.get("affiliation") or [])
            if affiliation.get("name")
        ))
        dates = crossref_dates(item)
        proof = item.get("_date_verification") or {}
        if proof.get("status") == "verified":
            dates["online_date"] = datetime.strptime(proof["online_date"], "%Y-%m-%d").date()
            dates["pub_date"] = dates["online_date"]
            dates["date_kind"] = "官网核实的在线发表日期"
            dates["online_date_source"] = proof["publisher_url"] + " · " + proof["date_label"]
        return Paper(
            source=self.name,
            journal=next(iter(item.get("container-title") or []), "期刊名未提供（按订阅 ISSN 检索）"),
            title=title,
            abstract=self._clean_text(item.get("abstract")),
            authors=[a for a in authors if a],
            affiliations=affiliations or None,
            url="https://doi.org/" + quote(item["DOI"], safe="/"),
            doi=item["DOI"].strip().lower(),
            online_date=dates["online_date"].isoformat() if dates["online_date"] else None,
            publication_date=dates["pub_date"].isoformat() if dates["pub_date"] else None,
            publication_date_kind=dates["date_kind"], publication_date_source=dates["online_date_source"],
            freshness=item.get("_freshness", "unverified"),
            date_verification=proof or None,
        )

    def mark_delivered(self, papers):
        if self.config.executor.debug or not hasattr(self, "_state"):
            return
        for paper in papers:
            if paper.source == self.name and paper.doi:
                assert_verified(paper.date_verification, paper.doi, paper.online_date, self._scan_date)
                self._state["seen"][paper.doi] = self._scan_date.isoformat()
                self._state.get("pending_date_checks", {}).pop(paper.doi, None)
        self._state["last_successful_fetch"] = self._scan_date.isoformat()
        self._save_state()

    def _save_state(self):
        if self.config.executor.debug:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._state_path.with_suffix(".tmp")
        temp.write_text(json_state(self._state), encoding="utf-8")
        temp.replace(self._state_path)
