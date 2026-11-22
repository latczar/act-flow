"""The Streamlit review console, driven headlessly with Streamlit's AppTest."""
from streamlit.testing.v1 import AppTest

from conftest import case_of, run


def open_console(monkeypatch):
    monkeypatch.setenv("ACT_NO_WATCH", "1")
    return AppTest.from_file("../console.py", default_timeout=60).run()


def test_console_correct_and_approve(env, monkeypatch):
    run("02-payroll-new-starter")
    ref = case_of("payroll_new_starter")["ref"]
    at = open_console(monkeypatch)
    assert not at.exception
    assert at.selectbox(key="ref").value == ref
    assert any("Waiting for the client" in i.value for i in at.info)

    # The client phoned with the missing date of birth: a reviewer records it.
    at.selectbox(key=f"correct-field-{ref}").set_value("date_of_birth")
    at.text_input(key=f"correct-value-{ref}").set_value("14 March 1998")
    at.text_input(key=f"correct-reason-{ref}").set_value("Client phoned with the date of birth")
    at.button(key=f"correct-btn-{ref}").click().run()
    assert not at.exception
    assert case_of(ref=ref)["status"] == "awaiting_review"

    at.button(key=f"approve-btn-{ref}").click().run()           # Casey is not the payroll lead
    assert any("payroll lead" in e.value for e in at.error)
    at.selectbox(key="user").set_value("alex.morgan").run()
    at.button(key=f"approve-btn-{ref}").click().run()
    assert not at.exception
    assert case_of(ref=ref)["status"] == "completed"


def test_other_views_render(env, monkeypatch):
    run("01-address-change")
    at = open_console(monkeypatch)
    at.radio(key="view").set_value("Systems").run()
    assert not at.exception
    at.radio(key="view").set_value("Audit log").run()
    assert not at.exception
    assert any("Hash chain intact" in s.value for s in at.success)
