"""Root conftest: config fixtures and shared helpers.

All mocking uses pytest monkeypatch + SimpleNamespace. No unittest.mock.
"""

import copy

import pytest
from hydra import compose, initialize_config_dir
from hydra.core.global_hydra import GlobalHydra
from pathlib import Path

_CONFIG_DIR = str(Path(__file__).resolve().parent.parent / "config")


@pytest.fixture(scope="session")
def _base_config():
    """Session-scoped Hydra config with all required values filled in.

    Never mutate this directly; use the function-scoped ``config`` fixture.
    """
    GlobalHydra.instance().clear()
    with initialize_config_dir(config_dir=_CONFIG_DIR, version_base=None):
        cfg = compose(
            config_name="default",
            overrides=[
                "zotero.user_id=000000",
                "zotero.api_key=fake-zotero-key",
                "zotero.include_path=null",
                "zotero.ignore_path=null",
                "email.sender=test@example.com",
                "email.receiver=test@example.com",
                "email.smtp_server=localhost",
                "email.smtp_port=1025",
                "email.sender_password=test",
                "llm.api.key=sk-fake",
                "llm.api.base_url=http://localhost:30000/v1",
                "llm.generation_kwargs.model=gpt-4o-mini",
                "reranker.api.key=sk-fake",
                "reranker.api.base_url=http://localhost:30000/v1",
                "reranker.api.model=text-embedding-3-large",
                "source.arxiv.category=[cs.AI,cs.CV]",
                "executor.source=[arxiv]",
                "executor.journals_only=false",
                "executor.reranker=api",
                "executor.debug=false",
                "executor.send_empty=false",
            ],
        )
    return cfg


@pytest.fixture()
def config(_base_config, tmp_path):
    """Function-scoped deep copy of the session config.

    Safe to mutate inside any test without polluting other tests.
    """
    cfg = copy.deepcopy(_base_config)
    cfg.source.journals.state_path = str(tmp_path / "journal-recommendations.json")
    return cfg


@pytest.fixture(autouse=True)
def _legacy_pipeline_publisher_stubs(request, monkeypatch):
    """Legacy pipeline cases stay offline; verifier tests use actual parser + fake HTTP."""
    if Path(request.module.__file__).name in {
        "test_journal_recommendation_policy.py", "test_journal_retriever.py",
    }:
        from tests.date_verification_helpers import offline_verify
        from zotero_arxiv_daily.publication_verifier import PublisherDateVerifier
        monkeypatch.setattr(PublisherDateVerifier, "verify", offline_verify)
