import html
import re
from time import sleep
from typing import Any

import requests
from loguru import logger

from .base import BaseRetriever


class CrossrefBase(BaseRetriever):
    """Shared HTTP and metadata helpers for Crossref sources."""

    request_headers = {
        "User-Agent": "zotero-arxiv-daily (https://github.com/TideDra/zotero-arxiv-daily)",
        "Accept": "application/json",
    }
    _tag = re.compile(r"<[^>]+>")

    def _get_json(self, params: dict[str, Any]) -> dict[str, Any]:
        retry_num = 10
        delay_time = 10
        for i in range(retry_num):
            try:
                response = requests.get(
                    self.api_url,
                    params=params,
                    headers=self.request_headers,
                    timeout=60,
                )
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                if i == retry_num - 1:
                    raise
                logger.warning(f"Failed to retrieve papers: {exc}. Retry in {delay_time} seconds.")
                sleep(delay_time)

    @classmethod
    def _clean_text(cls, text: str | None) -> str:
        if not text:
            return ""
        text = cls._tag.sub(" ", text)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _author_name(author: dict[str, Any]) -> str:
        if author.get("name"):
            return author["name"].strip()
        return f"{author.get('given', '')} {author.get('family', '')}".strip()
