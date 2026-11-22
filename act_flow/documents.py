"""Outgoing emails (written to an outbox folder as .eml files) and generated letters (PDF)."""
from __future__ import annotations

import json
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from fpdf import FPDF

from . import config, store
from .extraction import SCHEMAS, type_label

SIGN_OFF = f"Kind regards,\n\nClient Services\n{config.FIRM_NAME}"


def _latin1(text: str) -> str:
    table = {"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "..."}
    return "".join(table.get(ch, ch) for ch in text).encode("latin-1", "replace").decode("latin-1")


def write_letter(path: Path, title: str, paragraphs: list[str]) -> Path:
    pdf = FPDF(format="A4")
    pdf.set_margins(20, 20, 20)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 10, _latin1(config.FIRM_NAME), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 6, "Demonstration document - synthetic data", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 13)
    pdf.multi_cell(0, 8, _latin1(title))
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 11)
    for p in paragraphs:
        pdf.multi_cell(0, 6, _latin1(p))
        pdf.ln(3)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path


def letter_content(kind: str, case: dict, values: dict, client: dict) -> tuple[str, list[str]]:
    company = client.get("name", values.get("company_name", ""))
    contact = values.get("contact_name", "")
    if kind == "engagement_letter":
        return (f"Engagement letter: {company}", [
            config.uk_date(config.today()), f"Dear {contact},",
            f"Thank you for choosing {config.FIRM_NAME}. This letter sets out the work you have asked us to do "
            f"for {company}: {values.get('services', 'the services agreed')}.",
            "It also explains what we need from you, how we keep your information safe, and our fees, which "
            "your client manager will confirm before work starts.",
            "Please sign this letter electronically to confirm you are happy for us to begin.",
            f"Reference: {case['ref']}", SIGN_OFF])
    if kind == "welcome_letter":
        return (f"Welcome to {config.FIRM_NAME}", [
            f"Dear {contact},",
            f"Welcome to {config.FIRM_NAME}. Your client reference is {client.get('account_id', '')}.",
            "Your client manager will be in touch shortly to agree a timetable and the records we need.",
            "To send us anything, email instructions@act.example and include your reference in the subject line.",
            SIGN_OFF])
    raise ValueError(f"Unknown letter: {kind}")


# ---- outbound email ------------------------------------------------------------------------------

def email_body(kind: str, case: dict, values: dict, extra: dict) -> tuple[str, str]:
    """Templated client emails. Returns (subject, body)."""
    ref, label = case["ref"], type_label(case.get("instruction_type")).lower()
    if kind == "acknowledgement":
        return ("We've received your email", "\n".join([
            "Hello,", "",
            f"Thank you for your email. We've received it and logged it under reference {ref}.",
            "We'll let you know when it's done, or if we need anything else.", "",
            "If you reply about this, please keep the reference in the subject line.", "", SIGN_OFF]))
    if kind in ("request_info", "reminder"):
        items = "\n".join(f"- {i}" for i in extra["items"])
        opening = ("We're still waiting for the information below. " if kind == "reminder" else "") + \
            f"To complete your request ({label}), we need:"
        note = f"\n{extra['note']}\n" if extra.get("note") else ""
        return ("Information needed" if kind == "request_info" else "Reminder: information needed", "\n".join([
            "Hello,", "", opening, "", items, note,
            "Please reply to this email with the details, keeping the reference in the subject line.", "", SIGN_OFF]))
    if kind == "signature_request":
        return ("Please sign your engagement letter", "\n".join([
            "Hello,", "",
            "Thank you for choosing us. We've sent your engagement letter for electronic signature.",
            "Once it's signed we'll set up your services and your client manager will be in touch.", "", SIGN_OFF]))
    if kind == "confirmation":
        return ("Your request is complete", "\n".join(["Hello,", "", confirmation_text(case, values, extra), "",
                                                       f"Reference: {ref}", "", SIGN_OFF]))
    raise ValueError(f"Unknown email: {kind}")


def confirmation_text(case: dict, v: dict, extra: dict) -> str:
    t = case.get("instruction_type")
    if t == "client_details_change":
        return (f"We've updated the address we hold for you to {v.get('new_address_line1')}, {v.get('new_town')}, "
                f"{v.get('new_postcode')} in all our systems.")
    if t == "payroll_new_starter":
        return (f"We've added {v.get('employee_name')} to your payroll, starting {config.uk_date(v.get('start_date'))}. "
                "They'll be included in the next payroll run.")
    if t == "bank_details_change":
        return (f"We've updated the bank details we hold for you to the account ending {v.get('account_number', '')[-4:]}, "
                "after confirming the change with you by phone.")
    if t == "records_submission":
        purpose = "tax return" if v.get("purpose") == "tax_return" else "accounts"
        return (f"We've received and filed {extra.get('files', 0)} documents for your {v.get('period')} {purpose}. "
                "We'll be in touch if we need anything else.")
    if t == "new_client_onboarding":
        return "Thank you for signing your engagement letter. Your client manager will be in touch to get started."
    return "Your request is complete."


def send_email(con, case: dict, kind: str, values: dict | None = None, extra: dict | None = None) -> str:
    inbound = store.case_messages(con, case["ref"], "in")
    first = inbound[0]["headers"] if inbound else {}
    last_id = con.execute("SELECT message_id FROM messages WHERE case_ref=? AND direction='in' "
                          "ORDER BY received_at DESC, rowid DESC LIMIT 1", (case["ref"],)).fetchone()
    subject, body = email_body(kind, case, values or {}, extra or {})
    count = len(store.case_messages(con, case["ref"], "out")) + 1
    message_id = f"<{case['ref']}.{kind}.{count}@{config.ACT_DOMAIN}>"
    msg = EmailMessage()
    msg["From"] = f"{config.FIRM_NAME} Client Services <{config.MAILBOX}>"
    msg["To"] = first.get("from", case.get("sender", ""))
    msg["Subject"] = f"[{case['ref']}] {subject}"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id
    if last_id:
        msg["In-Reply-To"] = last_id["message_id"]
        msg["References"] = last_id["message_id"]
    msg.set_content(body)
    path = config.sub("outbox") / f"{case['ref']}-{count:02d}-{kind}.eml"
    path.write_bytes(bytes(msg))
    con.execute("INSERT INTO messages(message_id, case_ref, direction, path, received_at, sha256, headers) "
                "VALUES(?,?,?,?,?,?,?)", (message_id, case["ref"], "out", str(path), config.now_iso(), "",
                                          json.dumps({"to": msg["To"], "subject": msg["Subject"], "kind": kind,
                                                      "body": body})))
    store.audit(con, case["ref"], "system", "email_sent", kind=kind, to=msg["To"], message_id=message_id)
    return message_id


def field_prompt(itype: str, field_name: str | None) -> str:
    schema = SCHEMAS.get(itype)
    fd = schema.get(field_name) if schema and field_name else None
    return fd.description if fd else (field_name or "Further details").replace("_", " ").capitalize()
