"""Explicit publisher-proof stubs for offline pipeline tests."""
from datetime import datetime, timezone
from zotero_arxiv_daily.publication_verifier import exact_date, normalize_doi


def proof_for(doi, online, today=None):
    online = exact_date(online)
    return dict(status=("future" if today and online and online > today else "verified") if online else "unverified",
                doi=normalize_doi(doi), online_date=online.isoformat() if online else None,
                publisher_url="https://www.sciencedirect.com/science/article/pii/TEST",
                checked_at=datetime.now(timezone.utc).isoformat(), http_status=200,
                date_label="Available online", date_text=online.isoformat() if online else "",
                evidence_sha256="0" * 64, reason="offline publisher-page stub")


def offline_verify(self, doi, publisher_url=None, expected_online=None, today=None):
    return proof_for(doi, expected_online, today)
