import sqlite3

import pytest

from act_flow import store
from conftest import run


def test_audit_chain_is_append_only_and_tamper_evident(env):
    run("01-address-change", "04-bank-details-change")
    with store.session() as con:
        assert store.verify_audit(con)["ok"]
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("UPDATE audit SET actor='someone-else' WHERE seq=3")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("DELETE FROM audit WHERE seq=3")

    # Someone with direct database access removes the guard and edits history: the chain shows it.
    con = store.connect()
    con.execute("DROP TRIGGER audit_no_update")
    con.execute("UPDATE audit SET detail='{}' WHERE seq=5")
    con.commit()
    result = store.verify_audit(con)
    con.close()
    assert result == {"ok": False, "events": 5, "broken_at": 5}


def test_every_system_write_keeps_before_and_after(env):
    run("01-address-change")
    with store.session() as con:
        writes = store.case_writes(con, "ACT-2026-000001")
    crm = next(w for w in writes if w["system"] == "crm" and w["collection"] == "accounts")
    assert crm["before"]["address"]["postcode"] == "ZZ99 1HL"
    assert crm["after"]["address"]["postcode"] == "ZZ99 4QS"
