from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from loguru import logger

from .base import register_retriever
from .crossref_base import CrossrefBase
from ..protocol import Paper


@register_retriever("journals")
class JournalRetriever(CrossrefBase):
    """Collect journal articles first deposited during complete UTC days."""

    api_url = "https://api.crossref.org/works"

    def _retrieve_raw_papers(self):
        days = self.retriever_config.lookback_days
        issns = self.retriever_config.issns
        if not issns or not isinstance(days, int) or days < 1:
            raise ValueError("Configure source.journals.issns and a positive lookback_days.")
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=days)
        end = today - timedelta(days=1)
        records = {}
        for issn in issns:
            params = {
                "filter": f"type:journal-article,issn:{issn},from-created-date:{start},until-created-date:{end}",
                "select": "DOI,title,abstract,author,container-title",
                "rows": 100,
                "cursor": "*",
            }
            count = 0
            while True:
                message = self._get_json(params)["message"]
                items = message["items"]
                for item in items:
                    doi = item["DOI"].strip().lower()
                    if doi:
                        records[doi] = item
                count += len(items)
                if len(items) < params["rows"]:
                    break
                params["cursor"] = message["next-cursor"]
            logger.info(f"Crossref {issn} | {start} to {end} UTC | {count} records")
        return list(records.values())

    def convert_to_paper(self, item):
        title = self._clean_text(next(iter(item.get("title") or []), ""))
        if not title:
            return None
        authors = [self._author_name(a) for a in item.get("author", [])]
        return Paper(
            source=self.name,
            journal=next(iter(item.get("container-title") or []), "Crossref"),
            title=title,
            abstract=self._clean_text(item.get("abstract")),
            authors=[a for a in authors if a],
            url="https://doi.org/" + quote(item["DOI"], safe="/"),
        )
