"""Test-wide isolation for the training corpus.

Capture is on by default and hangs off `settings`, which `backend.corpus.store` binds at
import time. Without this fixture a test run writes into the developer's real
`data/corpus.db` - the first suite run put 236 runs in it - which both corrupts the corpus
with synthetic data and lets one test see another's rows.

Autouse rather than opt-in, because the modules that call the corpus (`chat_runner`,
`lang_graph_agent`, `api/tests`) are reached from most of the suite, and a fixture you have
to remember is one that gets forgotten.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_corpus(tmp_path, monkeypatch):
    from backend.config import settings as real_settings
    from backend.corpus import store

    # A real Settings object with only the path moved, so the fixture exercises the same
    # `corpus_enabled` logic production does rather than a stand-in that could drift.
    monkeypatch.setattr(
        store,
        "settings",
        real_settings.model_copy(update={"corpus_db_path": str(tmp_path / "corpus.db")}),
    )
    # Drop any connection a previous test cached, and clear a `_degrade` left by a test
    # that induced a failure on purpose.
    store.reset_for_tests()
    yield
    store.reset_for_tests()
