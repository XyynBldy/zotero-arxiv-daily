"""The deployed mail policy excludes preprints and keeps dates/failures visible."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from zotero_arxiv_daily.executor import Executor
from zotero_arxiv_daily.construct_email import render_email
from zotero_arxiv_daily.protocol import CorpusPaper, Paper
from zotero_arxiv_daily.retriever.journal_retriever import JournalRetriever
from tests.canned_responses import make_sample_paper


def test_journal_policy_overrides_a_legacy_mixed_source_config(config, monkeypatch):
    config.executor.journals_only = True
    config.executor.source = ["arxiv", "journals"]
    instantiated = []

    def retriever_cls(source):
        instantiated.append(source)
        return lambda cfg: SimpleNamespace()

    monkeypatch.setattr("zotero_arxiv_daily.executor.get_retriever_cls", retriever_cls)
    monkeypatch.setattr("zotero_arxiv_daily.executor.get_reranker_cls", lambda name: lambda cfg: None)
    monkeypatch.setattr("zotero_arxiv_daily.executor.OpenAI", lambda **kwargs: None)
    executor = Executor(config)
    assert instantiated == ["journals"]
    assert set(executor.retrievers) == {"journals"}


def setup_retriever(config, monkeypatch, tmp_path, items, state=None):
    import zotero_arxiv_daily.retriever.journal_retriever as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 17, 7, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(module, "datetime", Clock)
    config.source.journals.issns = ["1365-1609"]
    config.source.journals.state_path = str(tmp_path / "seen.json")
    if state:
        (tmp_path / "seen.json").write_text(json.dumps(state), encoding="utf8")
    retriever = JournalRetriever(config)
    calls = []

    def get_json(params):
        calls.append(dict(params))
        return {"message": {"items": items}}

    monkeypatch.setattr(retriever, "_get_json", get_json)
    return retriever, calls


def article(doi, online=None, created=None, published=None):
    result = {"DOI": doi, "title": [doi], "container-title": ["Subscribed Journal"]}
    for field, values in (("published-online", online), ("created", created), ("published", published)):
        if values:
            result[field] = {"date-parts": [values]}
    return result


def test_current_day_and_online_queries_are_limited_to_subscribed_issns(config, monkeypatch, tmp_path):
    retriever, calls = setup_retriever(config, monkeypatch, tmp_path, [article("10.1234/a", [2026, 11, 17])])
    result = retriever._retrieve_raw_papers()
    assert len(result) == 1
    assert all("issn:1365-1609" in c["filter"] for c in calls)
    assert any("from-online-pub-date:" in c["filter"] for c in calls)
    assert all("until-" in c["filter"] and ":2026-11-17" in c["filter"] for c in calls)
    paper = retriever.convert_to_paper(result[0])
    assert paper.online_date == "2026-11-17"
    assert paper.freshness == "recent_online"


def test_future_online_dates_are_excluded_and_future_issue_dates_are_unverified(config, monkeypatch, tmp_path):
    records = [article("10.1234/future", [2026, 11, 18], [2026, 11, 17]),
               article("10.1234/issue", created=[2026, 11, 17], published=[2027, 1, 1]),
               article("10.1234/backfill", [2026, 10, 1], [2026, 11, 17])]
    retriever, _ = setup_retriever(config, monkeypatch, tmp_path, records)
    result = retriever._retrieve_raw_papers()
    assert {r["DOI"] for r in result} == {"10.1234/issue", "10.1234/backfill"}
    papers = [retriever.convert_to_paper(r) for r in result]
    assert papers[0].online_date is None and papers[0].freshness == "unverified"
    assert papers[1].online_date == "2026-10-01" and papers[1].freshness == "late_record"


def test_recovery_uses_saved_progress_and_marks_only_delivered_dois(config, monkeypatch, tmp_path):
    state = {"schema": 1, "seen": {"10.1234/sent": "2026-11-17"}, "last_successful_fetch": "2026-11-01"}
    records = [article("10.1234/sent", [2026, 11, 17]), article("10.1234/new", [2026, 11, 17])]
    retriever, calls = setup_retriever(config, monkeypatch, tmp_path, records, state)
    result = retriever._retrieve_raw_papers()
    assert [r["DOI"] for r in result] == ["10.1234/new"]
    assert any("from-index-date:2026-10-31" in c["filter"] for c in calls)
    assert json.loads((tmp_path / "seen.json").read_text())["last_successful_fetch"] == "2026-11-01"
    retriever.mark_delivered([retriever.convert_to_paper(result[0])])
    after = json.loads((tmp_path / "seen.json").read_text())
    assert after["last_successful_fetch"] == "2026-11-17"
    assert after["seen"]["10.1234/new"] == "2026-11-17"


def test_debug_preview_does_not_write_delivery_state(config, monkeypatch, tmp_path):
    config.executor.debug = True
    retriever, _ = setup_retriever(config, monkeypatch, tmp_path, [article("10.1234/a", [2026, 11, 17])])
    result = retriever._retrieve_raw_papers()
    retriever.mark_delivered([retriever.convert_to_paper(result[0])])
    assert not (tmp_path / "seen.json").exists()


def test_email_displays_date_provenance_and_marks_original_abstract_on_failure():
    paper = make_sample_paper(source="journals", journal="Subscribed <Journal>",
                              online_date="2026-11-17", publication_date_source="Crossref published-online",
                              freshness="recent_online", tldr_status="abstract_fallback", tldr="Original abstract")
    html = render_email([paper])
    assert "Subscribed &lt;Journal&gt;" in html
    assert "在线发表：2026-11-17" in html and "Crossref published-online" in html
    assert "原文摘要（AI 生成失败）" in html
    assert "TLDR:" not in html
    assert "本次 1 篇 AI 摘要生成失败" in html


def test_email_does_not_label_a_future_issue_date_as_online_publication():
    paper = make_sample_paper(source="journals", publication_date="2027-01-01",
                              publication_date_kind="期刊记录日期", freshness="unverified")
    html = render_email([paper])
    assert "在线发表日期：待核实" in html
    assert "不作为最新在线发表依据" in html
    assert "在线发表：2027-01-01" not in html


def test_missing_author_affiliations_do_not_drop_a_journal_article(config):
    retriever = JournalRetriever(config)
    item = article("10.1234/no-affiliation", [2026, 11, 17])
    item["author"] = [{"given": "A", "family": "Researcher", "affiliation": None}]
    paper = retriever.convert_to_paper(item)
    assert paper.title == "10.1234/no-affiliation"
    assert paper.affiliations is None


def test_failed_llm_request_sets_fallback_status(monkeypatch):
    paper = make_sample_paper()
    def fail(*args): raise RuntimeError("offline test")
    monkeypatch.setattr(Paper, "_generate_tldr_with_llm", fail)
    assert paper.generate_tldr(None, {}) == paper.abstract
    assert paper.tldr_status == "abstract_fallback"


def executor_for_delivery(config, events, papers):
    executor = Executor.__new__(Executor)
    executor.config = config
    executor.journals_only = True
    executor.fetch_zotero_corpus = lambda: [CorpusPaper("ref", "abstract", datetime.now(), [])]
    executor.filter_corpus = lambda corpus: corpus
    executor.reranker = SimpleNamespace(rerank=lambda candidates, corpus: candidates)
    executor.openai_client = None
    executor.retrievers = {"journals": SimpleNamespace(
        retrieve_papers=lambda: papers, mark_delivered=lambda selected: events.append(("saved", selected)))}
    return executor


def test_delivery_state_is_written_only_after_successful_smtp(config, monkeypatch):
    paper = make_sample_paper(source="journals", abstract="", full_text=None)
    events = []
    executor = executor_for_delivery(config, events, [paper])
    monkeypatch.setattr("zotero_arxiv_daily.executor.send_email", lambda *args: events.append(("sent", None)))
    executor.run()
    assert [event[0] for event in events] == ["sent", "saved"]
    events.clear()
    def fail(*args): raise RuntimeError("SMTP offline")
    monkeypatch.setattr("zotero_arxiv_daily.executor.send_email", fail)
    with pytest.raises(RuntimeError, match="SMTP offline"):
        executor.run()
    assert not events


def test_recent_online_paper_is_selected_before_higher_scoring_old_backfill(config, monkeypatch):
    config.executor.max_paper_num = 1
    recent = make_sample_paper(source="journals", abstract="", full_text=None,
                              online_date="2026-11-17", freshness="recent_online", score=1)
    old = make_sample_paper(source="journals", abstract="", full_text=None,
                           online_date="2025-01-01", freshness="late_record", score=9)
    events = []
    executor = executor_for_delivery(config, events, [old, recent])
    monkeypatch.setattr("zotero_arxiv_daily.executor.send_email", lambda *args: None)
    executor.run()
    assert events == [("saved", [recent])]
