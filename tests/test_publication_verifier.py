"""Real publisher-page parser and both delivery gates; all HTTP is simulated."""
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from zotero_arxiv_daily.publication_verifier import PublisherDateVerifier, assert_verified
from tests.date_verification_helpers import proof_for

DOI = "10.1234/strict"
URL = "https://www.sciencedirect.com/science/article/pii/STRICT"
TODAY = date(2026, 11, 17)
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("strict_digest", ROOT / "scripts/journal_digest.py")
jd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jd)


def page(doi=DOI, date_text="17 November 2026", extra=""):
    return f'<html><meta name="citation_doi" content="{doi}"><section id="article-history">Available online {date_text}</section>{extra}</html>'


class Response:
    def __init__(self, text="", status=200, location=None):
        self.content = text.encode()
        self.status_code, self.encoding = status, "utf-8"
        self.headers = {"Location": location} if location else {}
        self.closed = False
    def iter_content(self, size): yield self.content
    def close(self): self.closed = True


class Session:
    def __init__(self, *responses): self.responses, self.calls = list(responses), []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception): raise value
        return value


def verify(text, expected=None, status=200, today=TODAY):
    return PublisherDateVerifier(session=Session(Response(text, status))).verify(DOI, URL, expected, today)


def test_exact_doi_and_article_history_date_produce_auditable_proof():
    proof = verify(page(), date(2026, 11, 17))
    assert proof["status"] == "verified"
    assert proof["online_date"] == "2026-11-17"
    assert proof["date_label"] == "Available online"
    assert len(proof["evidence_sha256"]) == 64
    assert_verified(proof, DOI, "2026-11-17", TODAY)


def test_explicit_online_meta_field_is_accepted():
    proof = verify(f'<meta name="citation_doi" content="{DOI}"><meta name="citation_online_date" content="2026/11/17">')
    assert proof["status"] == "verified" and proof["date_label"] == "citation_online_date"


@pytest.mark.parametrize("content", [
    '<section id="article-history">Received 10 October 2026; Accepted 1 November 2026</section>',
    '<meta name="citation_publication_date" content="2027-01-01">',
    '<script type="application/ld+json">{"datePublished":"2026-11-17"}</script>',
    '<section id="article-history">Available online November 2026</section>',
    '<aside>Related article: Available online 17 November 2026</aside>',
])
def test_ambiguous_partial_or_related_article_dates_never_pass(content):
    assert verify(f'<meta name="citation_doi" content="{DOI}">' + content)["status"] == "unverified"


def test_wrong_or_missing_article_doi_is_rejected():
    assert verify(page(doi="10.1234/other"))["status"] == "unverified"
    assert verify('<section id="article-history">Available online 17 November 2026</section>')["status"] == "unverified"


def test_disagreement_with_crossref_and_conflicting_publisher_dates_are_held():
    assert verify(page(), date(2026, 11, 16))["status"] == "conflict"
    extra = '<section class="publication-history">First online 16 November 2026</section>'
    assert verify(page(extra=extra))["status"] == "conflict"


def test_future_online_date_never_passes_delivery_gate():
    proof = verify(page(date_text="18 November 2026"))
    assert proof["status"] == "future"
    with pytest.raises(ValueError): assert_verified(proof, DOI, proof["online_date"], TODAY)


@pytest.mark.parametrize("response", [Response(status=403), Response(status=429), requests.Timeout("offline")])
def test_network_and_access_failures_never_fall_back_to_crossref(response):
    proof = PublisherDateVerifier(session=Session(response)).verify(DOI, URL, TODAY, TODAY)
    assert proof["status"] == "unverified"
    assert proof["online_date"] is None


def test_resolver_refresh_is_followed_only_to_an_allowed_publisher():
    refresh = '<meta http-equiv="refresh" content="2; url=\'' + URL + '\'">'
    session = Session(Response(refresh), Response(page()))
    verifier = PublisherDateVerifier(session=session)
    proof = verifier.verify(DOI, "https://linkinghub.elsevier.com/retrieve/pii/STRICT", TODAY, TODAY)
    assert proof["status"] == "verified"
    assert session.calls[1][0] == URL
    assert all(call[1]["allow_redirects"] is False for call in session.calls)


def test_unapproved_redirect_is_not_requested():
    session = Session(Response(status=302, location="https://untrusted.example/article"))
    proof = PublisherDateVerifier(session=session).verify(DOI, URL, TODAY, TODAY)
    assert proof["status"] == "unverified" and len(session.calls) == 1


def test_http_and_missing_doi_are_not_requested():
    session = Session()
    verifier = PublisherDateVerifier(session=session)
    assert verifier.verify(DOI, URL.replace("https:", "http:"))["status"] == "unverified"
    assert verifier.verify("", URL)["status"] == "unverified"
    assert session.calls == []


def test_budget_defers_without_reusing_a_different_articles_proof():
    session = Session(Response(page()))
    verifier = PublisherDateVerifier(session=session, max_checks=1)
    assert verifier.verify(DOI, URL, TODAY, TODAY)["status"] == "verified"
    assert verifier.verify(DOI, URL, TODAY, TODAY)["status"] == "verified"
    deferred = verifier.verify("10.1234/next", URL, TODAY, TODAY)
    assert deferred["status"] == "unverified" and not deferred["attempted"]
    assert len(session.calls) == 1


def test_stale_or_mismatched_receipts_are_rejected_before_sending():
    proof = proof_for(DOI, TODAY)
    proof["checked_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with pytest.raises(ValueError): assert_verified(proof, DOI, TODAY, TODAY)
    with pytest.raises(ValueError): assert_verified(proof_for(DOI, TODAY), "10.1234/other", TODAY, TODAY)
    with pytest.raises(ValueError): assert_verified(None, DOI, TODAY, TODAY)


def digest_paper(doi=DOI, title="strict article"):
    return jd.make_crossref_paper({"DOI": doi, "title": [title],
        "created": {"date-parts": [[2026, 11, 17]]},
        "resource": {"primary": {"URL": URL}}}, "J")


def wire_digest(monkeypatch, response_factory):
    monkeypatch.setattr(jd, "PublisherDateVerifier", lambda **kwargs: PublisherDateVerifier(session=Session(*response_factory())))


def test_digest_holds_unverified_titles_and_retries_after_metadata_window_expires(monkeypatch):
    paper = digest_paper()
    cfg = {"lookback_days": 7, "journals": [{"name": "J", "issn": "1365-1609"}]}
    state = {"seen": {}}
    monkeypatch.setattr(jd, "fetch_crossref", lambda *args: [paper])
    wire_digest(monkeypatch, lambda: [Response(status=403)])
    groups, keys, _ = jd.select_new(cfg, state, None, TODAY)
    assert groups["J"] == [] and keys == set()
    assert DOI in state["pending_date_checks"] and state["seen"] == {}
    assert "strict article" not in jd.render_html(groups, TODAY, 7)
    # A later run has no matching recent metadata but still retries its queue.
    state = json.loads(jd.json_state(state))
    monkeypatch.setattr(jd, "fetch_crossref", lambda *args: [])
    wire_digest(monkeypatch, lambda: [Response(page())])
    groups, keys, _ = jd.select_new(cfg, state, None, date(2026, 12, 1))
    assert groups["J"][0]["online_date"] == TODAY
    assert groups["J"][0]["first_seen"] == TODAY
    assert groups["J"][0]["freshness"] == "late_record"
    assert keys == {"doi:" + DOI}


def test_digest_smtp_failure_preserves_pending_queue_without_marking_delivery(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("journals:\n- name: J\n  issn: '1365-1609'\n", encoding="utf8")
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(jd, "fetch_crossref", lambda *args: [digest_paper()])
    wire_digest(monkeypatch, lambda: [Response(page())])
    def fail(*args): raise RuntimeError("SMTP failed")
    monkeypatch.setattr(jd, "send_email", fail)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", "")
    with pytest.raises(RuntimeError, match="SMTP failed"):
        jd.main(["--config", str(cfg), "--state", str(state_path), "--report-dir", str(tmp_path / "reports"), "--date", "2026-11-17"])
    state = json.loads(state_path.read_text(encoding="utf8"))
    assert state["seen"] == {} and "last_successful_fetch" not in state
    assert DOI in state["pending_date_checks"]
    assert state["date_verification_audit"][DOI]["status"] == "verified"


def test_final_digest_boundary_refuses_a_bypassed_unverified_record(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("journals:\n- name: J\n  issn: '1365-1609'\n", encoding="utf8")
    paper = digest_paper()
    paper.update(online_date=TODAY, freshness="recent_online")
    monkeypatch.setattr(jd, "select_new", lambda *args: ({"J": [paper]}, {"doi:" + DOI}, {"J": 1}))
    sent = []
    monkeypatch.setattr(jd, "send_email", lambda *args: sent.append(args))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", "")
    with pytest.raises(ValueError, match="官网核实"):
        jd.main(["--config", str(cfg), "--state", str(tmp_path / "state.json"), "--report-dir", str(tmp_path / "reports"), "--date", "2026-11-17"])
    assert not sent


def test_final_recommendation_boundary_refuses_an_unverified_article(config, monkeypatch):
    from tests.test_journal_recommendation_policy import executor_for_delivery
    from tests.canned_responses import make_sample_paper
    events = []
    paper = make_sample_paper(source="journals", abstract="", full_text=None, doi=DOI)
    executor = executor_for_delivery(config, events, [paper])
    monkeypatch.setattr("zotero_arxiv_daily.executor.send_email", lambda *args: events.append("sent"))
    with pytest.raises(ValueError, match="官网核实"):
        executor.run()
    assert not events


def test_recommendation_retriever_retains_blocked_records(config, monkeypatch):
    from zotero_arxiv_daily.retriever.journal_retriever import JournalRetriever
    import zotero_arxiv_daily.retriever.journal_retriever as module
    monkeypatch.setattr(module, "PublisherDateVerifier", lambda: PublisherDateVerifier(session=Session(Response(status=403))))
    retriever = JournalRetriever(config)
    item = {"DOI": DOI, "title": ["Held article"], "resource": {"primary": {"URL": URL}}}
    monkeypatch.setattr(retriever, "_get_json", lambda *args: {"message": {"items": [item]}})
    assert retriever._retrieve_raw_papers() == []
    state = json.loads(Path(config.source.journals.state_path).read_text(encoding="utf8"))
    assert DOI in state["pending_date_checks"] and state["seen"] == {}
    assert state["date_verification_audit"][DOI]["http_status"] == 403


def test_removed_subscription_does_not_reenter_from_pending_queue(config, monkeypatch):
    from zotero_arxiv_daily.retriever.journal_retriever import JournalRetriever
    state_path = Path(config.source.journals.state_path)
    state_path.write_text(json.dumps({"seen": {}, "pending_date_checks": {
        DOI: {"DOI": DOI, "title": ["Unsubscribed"], "_subscribed_issn": "0000-0000"}
    }}), encoding="utf8")
    retriever = JournalRetriever(config)
    monkeypatch.setattr(retriever, "_get_json", lambda *args: {"message": {"items": []}})
    assert retriever._retrieve_raw_papers() == []
    assert retriever._state["pending_date_checks"] == {}
