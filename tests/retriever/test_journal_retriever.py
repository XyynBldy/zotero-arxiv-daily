from datetime import datetime

import numpy as np

from zotero_arxiv_daily.construct_email import render_email
from zotero_arxiv_daily.protocol import CorpusPaper
from zotero_arxiv_daily.reranker.base import BaseReranker
from zotero_arxiv_daily.retriever import get_retriever_cls


def test_journal_records_reach_ranking_and_email(config, monkeypatch):
    config.source.journals.issns = ["1365-1609", "0723-2632"]
    retriever = get_retriever_cls("journals")(config)
    first = {"DOI": "10.1234/a", "title": ["Pillar collapse"], "container-title": ["Journal A"]}
    second = dict(first, DOI="10.1234/b", abstract="<p>Rock strength.</p>")
    monkeypatch.setattr(retriever, "_get_json", lambda params: {"message": {"items": [first, second]}})
    monkeypatch.setattr("zotero_arxiv_daily.retriever.base.sleep", lambda _: None)
    papers = retriever.retrieve_papers()
    assert len(papers) == 2
    class FixtureReranker(BaseReranker):
        def get_similarity_score(self, texts, corpus):
            assert texts == ["Pillar collapse", "Rock strength."]
            return np.array([[0.9], [0.7]])

    ranker = FixtureReranker(config)
    ranked = ranker.rerank(papers, [CorpusPaper("Reference", "Coal pillar", datetime.now(), [])])
    assert ranked[0].generate_tldr(None, {}).startswith("Crossref 未提供摘要")
    html = render_email(ranked)
    assert 'href="https://doi.org/10.1234/a"' in html
    assert "Journal A" in html and "按标题匹配" in html
