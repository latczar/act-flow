"""End-to-end scenarios: each sample email through intake, AI extraction, validation, routing,
approvals and system updates."""
import pytest

from act_flow import config, orchestrator, review, samples, store, systems
from conftest import case_of, latest_case, outbox, run


def test_address_change_runs_straight_through(env):
    systems.system("accounting").inject_fault("transient", 1)       # one blip, recovered by retry
    run("01-address-change")
    case = case_of("client_details_change")
    assert case["status"] == "completed"
    assert case["data"]["straight_through"] is True
    assert systems.system("crm").get("accounts", "HLB001")["address"]["postcode"] == "ZZ99 4QS"
    assert systems.system("accounting").get("contacts", "acc-hlb001")["Addresses"][0]["PostalCode"] == "ZZ99 4QS"
    assert systems.system("tax").get("clients", "HLB001")["postcode"] == "ZZ99 4QS"
    assert systems.system("payroll").get("employers", "HLB001")["postcode"] == "ZZ99 4QS"
    assert outbox(case["ref"]) == ["acknowledgement", "confirmation"]
    with store.session() as con:
        events = [e["event"] for e in store.timeline(con, case["ref"])]
    assert "system_call_failed" in events                           # the blip is on the record


def test_duplicate_email_is_ignored(env):
    run("01-address-change")
    run("01-address-change")
    with store.session() as con:
        assert len(store.list_cases(con)) == 1
        assert con.execute("SELECT COUNT(*) FROM audit WHERE event='duplicate_email_ignored'").fetchone()[0] == 1


def test_payroll_starter_missing_info_then_reply_then_approval(env):
    run("02-payroll-new-starter")
    case = case_of("payroll_new_starter")
    assert case["status"] == "awaiting_client"
    with store.session() as con:
        request = store.case_messages(con, case["ref"], "out")[-1]["headers"]["body"]
    assert "date of birth" in request.lower()

    samples.send_reply(case["ref"], "Hi,\n\nSam's date of birth is 14 March 1998.\n\nThanks,\nHannah", "<reply-1@harbourlane.example>")
    orchestrator.process_all()
    case = case_of(ref=case["ref"])
    assert case["status"] == "awaiting_review"
    assert case["data"]["review"]["roles_needed"] == ["payroll"]
    assert case["data"]["validation"]["fields"]["date_of_birth"]["source"] == "reply1"

    with pytest.raises(review.ReviewError, match="payroll lead"):
        review.approve(case["ref"], "casey.brown")                    # wrong role
    review.approve(case["ref"], "alex.morgan", "Checked")
    case = case_of(ref=case["ref"])
    assert case["status"] == "completed"
    emp = systems.system("payroll").get("employees", f"HLB001-{case['ref'][-6:]}")
    assert emp["date_of_birth"] == "1998-03-14" and emp["annual_salary"] == 28500
    tasks = systems.system("crm").find("tasks", case_ref=case["ref"])
    assert tasks and tasks[0]["owner"] == "alex.morgan"
    files = systems.system("dms").find("files", case_ref=case["ref"])
    assert [f["folder"] for f in files] == ["Payroll"]


def test_onboarding_needs_compliance_then_signature(env):
    run("03-new-client-enquiry")
    case = case_of("new_client_onboarding")
    assert case["status"] == "awaiting_review"
    checks = {c["check"]: c["result"] for c in case["data"]["validation"]["checks"]}
    assert checks["Company found at Companies House"] == "pass"
    assert checks["Identity document matches the contact"] == "pass"
    assert outbox(case["ref"]) == ["acknowledgement"]                 # new client, verified sender
    review.approve(case["ref"], "jordan.lee", "ID checked")
    case = case_of(ref=case["ref"])
    assert case["status"] == "awaiting_signature"
    cid = case["client_id"]
    assert systems.system("crm").get("accounts", cid)["name"] == "Riverside Joinery Ltd"
    assert systems.system("tax").get("clients", cid)
    letter = systems.system("dms").get("files", f"{cid}-{case['ref']}-engagement_letter")
    assert letter["signature_status"] == "sent for e-signature"
    assert (config.data_dir() / letter["path"]).exists()
    review.signature_received(case["ref"])
    assert case_of(ref=case["ref"])["status"] == "completed"
    assert systems.system("dms").get("files", letter["file_id"])["signature_status"] == "signed"


def test_bank_change_needs_two_people_and_a_call_back(env):
    run("04-bank-details-change")
    ref = case_of("bank_details_change")["ref"]
    assert case_of(ref=ref)["status"] == "awaiting_review"
    with pytest.raises(review.ReviewError, match="call the client"):
        review.approve(ref, "casey.brown")
    review.approve(ref, "casey.brown", "Called", callback_confirmed=True)
    assert case_of(ref=ref)["status"] == "awaiting_review"
    with pytest.raises(review.ReviewError, match="already approved"):
        review.approve(ref, "casey.brown", callback_confirmed=True)
    with pytest.raises(review.ReviewError, match="finance"):
        review.approve(ref, "jordan.lee", callback_confirmed=True)
    review.approve(ref, "robin.shah", callback_confirmed=True)
    assert case_of(ref=ref)["status"] == "completed"
    bank = systems.system("crm").get("accounts", "HLB001")["bank"]
    assert bank["sort_code"] == "12-34-56" and bank["verified_by"] == ["casey.brown", "robin.shah"]
    assert systems.system("accounting").get("contacts", "acc-hlb001")["BankAccountDetails"] == "12-34-56 12345678"


def test_correction_clears_approvals_and_corrector_cannot_approve(env):
    run("04-bank-details-change")
    ref = case_of("bank_details_change")["ref"]
    review.approve(ref, "casey.brown", callback_confirmed=True)
    review.correct(ref, "robin.shah", "account_number", "12345679", "Client read the number out on the phone")
    case = case_of(ref=ref)
    assert case["status"] == "awaiting_review" and case["data"]["review"]["approvals"] == []
    assert case["data"]["validation"]["fields"]["account_number"]["value"] == "12345679"
    with pytest.raises(review.ReviewError, match="changed values"):
        review.approve(ref, "robin.shah", callback_confirmed=True)


def test_spoofed_sender_is_held_and_never_answered(env):
    run("05-suspicious-bank-change")
    case = latest_case()
    assert case["status"] == "held_security"
    assert "extraction" not in case["data"]                         # the email was never even read by the AI
    assert outbox(case["ref"]) == []                                # no auto-reply to a spoofed address
    with pytest.raises(review.ReviewError, match="client manager"):
        review.release(case["ref"], "alex.morgan", "checked")
    # Even if someone releases it, an unknown sender's bank change cannot be approved.
    review.release(case["ref"], "casey.brown", "Looked at it")
    case = case_of(ref=case["ref"])
    assert case["status"] == "awaiting_review"
    assert case["instruction_type"] == "bank_details_change"
    assert any("ignored" in r for r in case["data"]["extraction"]["other_requests"])
    with pytest.raises(review.ReviewError, match="not a known contact"):
        review.approve(case["ref"], "casey.brown", callback_confirmed=True)
    with store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0


def test_outage_pauses_case_and_resumes_without_repeating_work(env):
    systems.system("tax").inject_fault("transient", 5)
    run("06-records-tax-return")
    case = case_of("records_submission")
    assert case["status"] == "exception"
    assert "did not respond" in case["reason"]
    with store.session() as con:
        steps = {s["op"]: s for s in store.case_steps(con, case["ref"])}
        writes_before = con.execute("SELECT COUNT(*) FROM writes").fetchone()[0]
    assert steps["dms.file_attachments"]["status"] == "done"
    assert steps["tax.open_return"]["status"] == "failed" and steps["tax.open_return"]["attempts"] == 3
    systems.system("tax").clear_faults()
    review.retry(case["ref"], "casey.brown")
    assert case_of(ref=case["ref"])["status"] == "completed"
    assert systems.system("tax").get("returns", "FST001-SA-2025-26")["status"] == "records received"
    assert len(systems.system("dms").find("files", case_ref=case["ref"])) == 2   # filed once, not twice
    with store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM writes WHERE system='dms' AND case_ref=?",
                           (case["ref"],)).fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM writes").fetchone()[0] > writes_before


def test_cancel_undoes_every_change(env):
    systems.system("tax").inject_fault("transient", 5)
    run("06-records-tax-return")
    ref = case_of("records_submission")["ref"]
    assert systems.system("dms").find("files", case_ref=ref)
    print(review.cancel(ref, "casey.brown", "Client sent the wrong year"))
    assert case_of(ref=ref)["status"] == "cancelled"
    assert systems.system("dms").find("files", case_ref=ref) == []


def test_unclear_email_goes_to_triage_and_can_be_classified(env):
    raw = samples._email("Hannah Lane", "hannah@harbourlane.example", "Quick question",
                         "Hi, could you give me a call when you get a moment?\n\nHannah", "<sample-unclear@harbourlane.example>")
    (config.sub("inbox") / "unclear.eml").write_bytes(raw)
    orchestrator.process_all()
    case = latest_case()
    assert case["status"] == "awaiting_review" and case["instruction_type"] is None
    with pytest.raises(review.ReviewError, match="instruction type"):
        review.approve(case["ref"], "casey.brown")
    review.reject(case["ref"], "casey.brown", "Not an instruction - passed to the client manager to call back")
    assert case_of(ref=case["ref"])["status"] == "rejected"


def test_reminders_then_escalation(env, monkeypatch):
    run("02-payroll-new-starter")
    ref = case_of("payroll_new_starter")["ref"]
    for day, expected in (("2026-11-26", 2), ("2026-12-02", 3), ("2026-12-07", 3)):
        monkeypatch.setenv("ACT_TODAY", day)
        orchestrator.process_all()
        assert len(outbox(ref)) == 1 + expected                       # acknowledgement + request + reminders
    assert systems.system("crm").get("tasks", f"TASK-{ref}-chase")["owner"] == "casey.brown"
