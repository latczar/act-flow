import pytest

from act_flow import orchestrator, samples, store, systems


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fresh data folder with seeded systems and sample emails, on a fixed Monday."""
    monkeypatch.setenv("ACT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ACT_TODAY", "2026-11-23")
    monkeypatch.setenv("ACT_RETRY_DELAY", "0")
    monkeypatch.setenv("ACT_EXTRACTOR", "rules")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    orchestrator._breakers.clear()
    systems.seed()
    samples.write_samples()
    return tmp_path / "data"


def run(*names, extractor=None):
    for n in names:
        samples.send(n)
    orchestrator.process_all(extractor)


def case_of(itype=None, ref=None):
    with store.session() as con:
        if ref:
            return store.get_case(con, ref)
        row = con.execute("SELECT ref FROM cases WHERE instruction_type=? ORDER BY ref DESC LIMIT 1", (itype,)).fetchone()
        return store.get_case(con, row["ref"]) if row else None


def latest_case():
    with store.session() as con:
        return store.list_cases(con)[0]


def outbox(ref):
    with store.session() as con:
        return [m["headers"]["kind"] for m in store.case_messages(con, ref, "out")]
