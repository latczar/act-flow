"""Validation: format, grounding, cross-document, system/register, business-rule and duplicate checks.

Produces, for every schema field, a normalised value, a final confidence and any
issues, plus a list of named checks for the review screen. It never changes any
system; it only reports.
"""
from __future__ import annotations

import datetime as dt
import re

from . import config, store, systems
from .extraction import SCHEMAS, CHOICES, Source, parse_date, source_text

POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$")
NI_RE = re.compile(r"^(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]\d{6}[A-D]$")
COMPANY_RE = re.compile(r"^(?:\d{8}|(?:SC|NI|OC|SO|NC|R0)\d{6})$")
ALNUM_KINDS = {"ni", "postcode", "sort_code", "account_number", "company_number", "money"}


def _alnum(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


# ---- format ---------------------------------------------------------------------------

def normalise(kind: str, value: str, name: str = "") -> tuple[str | None, str | None]:
    """Return (normalised value, error message or None)."""
    v = (value or "").strip()
    if not v:
        return None, "missing"
    if kind == "date":
        d = parse_date(v)
        return (d.isoformat(), None) if d else (v, "This is not a date we can read.")
    if kind == "money":
        try:
            amount = float(re.sub(r"[£,\s]", "", v))
        except ValueError:
            return v, "This is not an amount of money."
        if amount <= 0:
            return v, "The amount must be more than zero."
        return (f"{amount:.0f}" if amount == int(amount) else f"{amount:.2f}"), None
    if kind == "ni":
        n = re.sub(r"\s", "", v).upper()
        if config.allow_test_ids() and re.fullmatch(r"QQ\d{6}[A-D]", n):
            return n, None
        return (n, None) if NI_RE.match(n) else (n, "This is not a valid National Insurance number.")
    if kind == "postcode":
        p = re.sub(r"\s+", "", v).upper()
        p = f"{p[:-3]} {p[-3:]}" if len(p) > 3 else p
        return (p, None) if POSTCODE_RE.match(p) else (p, "This is not a valid UK postcode.")
    if kind == "sort_code":
        d = re.sub(r"\D", "", v)
        return (f"{d[:2]}-{d[2:4]}-{d[4:]}", None) if len(d) == 6 else (v, "A sort code has six digits.")
    if kind == "account_number":
        d = re.sub(r"\D", "", v)
        return (d, None) if len(d) == 8 else (v, "A UK account number has eight digits.")
    if kind == "company_number":
        c = re.sub(r"\s", "", v).upper()
        return (c, None) if COMPANY_RE.match(c) else (c, "This is not a valid company number.")
    if kind == "statement":
        s = v.upper()
        return (s, None) if s in {"A", "B", "C"} else (s, "The starter statement must be A, B or C.")
    if kind == "choice":
        c = v.lower().replace(" ", "_")
        return (c, None) if c in CHOICES.get(name, []) else (c, f"Expected one of: {', '.join(CHOICES.get(name, []))}.")
    if len(v) > 200:
        return v[:200], "This value is too long."
    return v, None


# ---- grounding ----------------------------------------------------------------------

def grounded(kind: str, value: str, quote: str | None, text: str | None) -> str | None:
    """Return an error if the value is not supported by the quoted source text."""
    if not quote:
        return "No supporting text was given for this value."
    if text is None:
        return "The source this value was said to come from does not exist."
    if _ws(quote) not in _ws(text):
        return "The supporting text does not appear in the source."
    if kind in ALNUM_KINDS:
        if _alnum(value) not in _alnum(quote):
            return "The value does not match the supporting text."
    elif kind == "date":
        d = dt.date.fromisoformat(value)
        if not re.search(rf"(?<!\d)0?{d.day}(?!\d)", quote):
            return "The date does not match the supporting text."
    elif kind == "text":
        tokens = [t for t in re.findall(r"[a-z0-9]+", value.lower()) if len(t) > 1]
        if any(t not in quote.lower() for t in tokens):
            return "The value does not match the supporting text."
    return None


def same_person(a: str | None, b: str | None) -> bool:
    ta = re.findall(r"[a-z]+", (a or "").lower())
    tb = re.findall(r"[a-z]+", (b or "").lower())
    return bool(ta and tb) and ta[0] == tb[0] and ta[-1] == tb[-1]


def _company_key(name: str) -> str:
    return re.sub(r"\b(ltd|limited|llp|plc)\b", "", (name or "").lower()).replace(".", "").strip()


# ---- main entry ------------------------------------------------------------------------

def validate(con, case: dict, extraction: dict, sources: list[Source], corrections: dict) -> dict:
    itype = extraction.get("instruction_type")
    result = {"fields": {}, "issues": [], "checks": [], "confidence": 0.0}
    if itype not in SCHEMAS:
        return result
    schema = SCHEMAS[itype]
    fields, issues, checks = result["fields"], result["issues"], result["checks"]

    def issue(field_name, severity, message, client_can_fix=False):
        issues.append({"field": field_name, "severity": severity, "message": message, "client_can_fix": client_can_fix})

    def check(name, ok, detail):
        checks.append({"check": name, "result": "pass" if ok else "fail", "detail": detail})

    # 1. format, completeness and grounding, field by field
    for fd in schema.fields:
        raw = dict(extraction["fields"].get(fd.name) or {})
        if fd.name in corrections:
            c = corrections[fd.name]
            raw = {"value": c["value"], "source": "reviewer", "quote": None, "confidence": 1.0,
                   "corrected_by": c["user"]}
        entry = {"value": None, "raw": raw.get("value"), "source": raw.get("source"), "quote": raw.get("quote"),
                 "confidence": float(raw.get("confidence") or 0.0), "status": "ok", "messages": []}
        if raw.get("corrected_by"):
            entry["corrected_by"] = raw["corrected_by"]
        value, err = normalise(fd.kind, raw.get("value") or "", fd.name)
        if err == "missing":
            entry["status"], entry["confidence"] = ("missing" if fd.required else "not given"), 0.0
            if fd.required:
                issue(fd.name, "missing", f"{fd.description} is missing.", fd.client_can_supply)
        elif err:
            entry.update(status="invalid", value=value, confidence=0.0)
            entry["messages"].append(err)
            issue(fd.name, "invalid", f"{fd.description}: {err}", fd.client_can_supply)
        else:
            entry["value"] = value
            if entry["source"] not in ("reviewer", "crm_match"):
                g = grounded(fd.kind, value, entry["quote"], source_text(sources, entry["source"]))
                if g:
                    entry.update(status="unverified", confidence=0.0)
                    entry["messages"].append(g)
                    issue(fd.name, "unverified", f"{fd.description}: {g}")
        fields[fd.name] = entry

    v = {k: e["value"] for k, e in fields.items()}

    def flag(field_name, severity, message, cap=0.5):
        issue(field_name, severity, message)
        if field_name in fields:
            fields[field_name]["confidence"] = min(fields[field_name]["confidence"], cap)
            fields[field_name]["messages"].append(message)

    # 2. the client
    client = systems.system("crm").get("accounts", case["client_id"]) if case.get("client_id") else None
    if itype != "new_client_onboarding":
        check("Client identified", bool(client),
              f"Matched to {client['name']} ({client['account_id']})" if client else "No client record matches the sender.")
        if not client:
            issue(None, "unknown_client", "The sender is not a known contact for any client.")

    # 3. cross-document, systems, registers and business rules, per type
    today = config.today()
    if itype == "payroll_new_starter" and client:
        checklist = next((s for s in sources if s.kind == "doc" and s.doc_type == "new_starter_checklist"), None)
        if checklist and v.get("employee_name"):
            ok = all(t in checklist.text.lower() for t in re.findall(r"[a-z]+", v["employee_name"].lower()))
            check("Name matches the starter checklist", ok, v["employee_name"])
            if not ok:
                flag("employee_name", "mismatch", "The name in the email is not the name on the starter checklist.")
        employees = systems.system("payroll").find("employees", employer_ref=client["account_id"])
        dup = [e for e in employees if (v.get("ni_number") and e.get("ni_number") == v["ni_number"])
               or same_person(f"{e['forename']} {e['surname']}", v.get("employee_name"))]
        check("Not already on payroll", not dup, f"{len(employees)} employees checked")
        if dup:
            flag("employee_name", "duplicate", "An employee with this name or NI number is already on payroll.")
        if v.get("start_date"):
            start = dt.date.fromisoformat(v["start_date"])
            if start < today - dt.timedelta(days=30):
                flag("start_date", "warning", "The start date is more than 30 days ago.")
            pay_date = config.next_pay_date(client.get("pay_day", 25), today)
            cutoff = config.add_working_days(pay_date, -2)
            late = start <= pay_date and today > cutoff
            check("Arrived before the payroll cut-off", not late,
                  f"Next pay date {config.uk_date(pay_date)}, cut-off {config.uk_date(cutoff)}")
            if late:
                flag("start_date", "warning", "This change arrived after the cut-off for the next payroll run.", 0.6)

    if itype == "new_client_onboarding":
        ch = systems.companies_house_lookup(v["company_number"]) if v.get("company_number") else None
        if v.get("company_number"):
            check("Company found at Companies House", bool(ch), v["company_number"])
            if not ch:
                flag("company_number", "mismatch", "Companies House has no company with this number.", 0.0)
        if ch:
            name_ok = _company_key(ch["name"]) == _company_key(v.get("company_name") or "")
            check("Company name matches Companies House", name_ok, ch["name"].title())
            if not name_ok:
                flag("company_name", "mismatch", f"Companies House lists this number as {ch['name'].title()}.")
            check("Company is active", ch["status"] == "active", ch["status"])
            if ch["status"] != "active":
                flag("company_number", "mismatch", f"Companies House shows the company as {ch['status']}.")
            officer = any(same_person(o, v.get("contact_name")) for o in ch.get("officers", []))
            check("Contact is a listed officer", officer, ", ".join(ch.get("officers", [])))
            if not officer:
                flag("contact_name", "warning", "The contact is not listed as an officer at Companies House.")
        existing = systems.system("crm").find("accounts", company_number=v.get("company_number") or "-")
        check("Not already a client", not existing, "CRM searched by company number")
        if existing:
            flag("company_number", "duplicate", f"This company is already a client ({existing[0]['account_id']}).")
        if v.get("director_id_name") and v.get("contact_name"):
            ok = same_person(v["director_id_name"], v["contact_name"])
            check("Identity document matches the contact", ok, v["director_id_name"])
            if not ok:
                flag("director_id_name", "mismatch", "The name on the identity document does not match the contact.")
        if v.get("proof_of_address_name") and v.get("contact_name"):
            ok = same_person(v["proof_of_address_name"], v["contact_name"])
            check("Proof of address matches the contact", ok, v["proof_of_address_name"])
            if not ok:
                flag("proof_of_address_name", "mismatch", "The name on the proof of address does not match the contact.")

    if itype == "bank_details_change" and client and v.get("account_name"):
        ok = _company_key(v["account_name"]) == _company_key(client["name"])
        check("Account name matches the client", ok, f"{v['account_name']} / {client['name']}")
        if not ok:
            flag("account_name", "mismatch", "The account name is not the client's name.")

    if itype == "client_details_change" and client and v.get("new_postcode"):
        same = (client.get("address") or {}).get("postcode") == v["new_postcode"]
        check("Address is a change", not same, f"Current postcode {(client.get('address') or {}).get('postcode')}")
        if same:
            flag("new_postcode", "warning", "This is the address we already hold.")
        if v.get("effective_date") and dt.date.fromisoformat(v["effective_date"]) < today - dt.timedelta(days=30):
            flag("effective_date", "warning", "The effective date is more than 30 days ago.")

    if itype == "records_submission" and client:
        check("Client uses this service", True, ", ".join(client.get("services", [])))

    # 4. duplicates: same attachment already received on another recent case
    since = (today - dt.timedelta(days=30)).isoformat()
    for d in store.case_documents(con, case["ref"]):
        row = con.execute("SELECT case_ref FROM documents WHERE sha256=? AND case_ref<>? AND received_at>=? LIMIT 1",
                          (d["sha256"], case["ref"], since)).fetchone()
        if row:
            issue(None, "duplicate", f"{d['filename']} was already received on case {row['case_ref']}.")
    check("No duplicate attachments", not any(i["severity"] == "duplicate" and i["field"] is None for i in issues),
          "Compared file hashes from the last 30 days")

    # 5. overall confidence: as strong as the weakest required field
    required = [fields[f.name]["confidence"] for f in schema.fields if f.required]
    result["confidence"] = round(min(required) if required else 1.0, 3)
    return result
