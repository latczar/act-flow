"""One adapter per business system.

The workflow speaks ACT's own data model (client, contact, employee, document, task);
each adapter translates that into the target system's own record shape. Every write
carries an idempotency key and stores the record's before and after values, so a
retry never duplicates anything and a cancelled case can be undone.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from dataclasses import dataclass

from . import config, documents, store, systems
from .routing import CONFIG


@dataclass
class Ctx:
    con: object
    case: dict
    idem: str

    @property
    def values(self) -> dict:
        return {k: e["value"] for k, e in self.case["data"]["validation"]["fields"].items()}

    @property
    def client_id(self) -> str | None:
        return self.case.get("client_id")

    def client(self) -> dict:
        return systems.system("crm").get("accounts", self.client_id) or {}

    def write(self, sysname: str, collection: str, rec_id: str, data: dict, key: str = "") -> dict:
        return write_once(self.con, self.case["ref"], f"{self.idem}{key}", sysname, collection, rec_id, data)


def write_once(con, case_ref, idem_key, sysname, collection, rec_id, after) -> dict:
    done = con.execute("SELECT * FROM writes WHERE idem_key=?", (idem_key,)).fetchone()
    if done:
        return {"system": sysname, "id": rec_id, "skipped": "already done"}
    target = systems.system(sysname)
    before = target.get(collection, rec_id)
    target.put(collection, rec_id, after)
    con.execute("INSERT INTO writes(idem_key, case_ref, system, collection, record_id, before, after, at) "
                "VALUES(?,?,?,?,?,?,?,?)", (idem_key, case_ref, sysname, collection, rec_id,
                                            store.dumps(before), store.dumps(after), config.now_iso()))
    con.commit()
    return {"system": sysname, "id": rec_id, "created": before is None}


def undo_case(con, case_ref: str, actor: str) -> list[str]:
    """Compensate: put every record this case changed back as it was, newest change first."""
    undone = []
    rows = con.execute("SELECT * FROM writes WHERE case_ref=? AND undone=0 ORDER BY rowid DESC", (case_ref,)).fetchall()
    for w in rows:
        target = systems.system(w["system"])
        before = json.loads(w["before"])
        if before is None:
            target.delete(w["collection"], w["record_id"])
        else:
            target.put(w["collection"], w["record_id"], before)
        con.execute("UPDATE writes SET undone=1 WHERE idem_key=?", (w["idem_key"],))
        store.audit(con, case_ref, actor, "change_undone", system=w["system"], record=w["record_id"],
                    restored="deleted" if before is None else "previous values")
        undone.append(f"{w['system']}:{w['record_id']}")
    return undone


# ---- address changes ---------------------------------------------------------------------

def crm_update_address(ctx: Ctx) -> dict:
    v, acc = ctx.values, ctx.client()
    acc["address"] = {"line1": v["new_address_line1"], "town": v["new_town"], "postcode": v["new_postcode"]}
    ctx.write("crm", "accounts", ctx.client_id, acc)
    return {"summary": f"CRM address set to {v['new_address_line1']}, {v['new_town']}, {v['new_postcode']}"}


def accounting_update_address(ctx: Ctx) -> dict:
    v = ctx.values
    contacts = systems.system("accounting").find("contacts", AccountNumber=ctx.client_id)
    if not contacts:
        return {"summary": "No accounting contact for this client, nothing to change"}
    c = contacts[0]
    c["Addresses"] = [{"AddressType": "POBOX", "AddressLine1": v["new_address_line1"], "City": v["new_town"],
                       "PostalCode": v["new_postcode"]}]
    ctx.write("accounting", "contacts", c["ContactID"], c)
    return {"summary": f"Accounting contact {c['ContactID']} address updated"}


def tax_update_address(ctx: Ctx) -> dict:
    v = ctx.values
    rec = systems.system("tax").get("clients", ctx.client_id)
    if not rec:
        return {"summary": "No tax record for this client, nothing to change"}
    rec.update(address=f"{v['new_address_line1']}, {v['new_town']}", postcode=v["new_postcode"])
    ctx.write("tax", "clients", ctx.client_id, rec)
    return {"summary": "Tax client address updated"}


def payroll_update_employer_address(ctx: Ctx) -> dict:
    v = ctx.values
    rec = systems.system("payroll").get("employers", ctx.client_id)
    if not rec:
        return {"summary": "Not a payroll client, nothing to change"}
    rec.update(address_lines=[v["new_address_line1"], v["new_town"]], postcode=v["new_postcode"])
    ctx.write("payroll", "employers", ctx.client_id, rec)
    return {"summary": "Payroll employer address updated"}


# ---- payroll ------------------------------------------------------------------------------

def payroll_create_employee(ctx: Ctx) -> dict:
    v = ctx.values
    parts = v["employee_name"].split()
    emp_id = f"{ctx.client_id}-{ctx.case['ref'][-6:]}"
    ctx.write("payroll", "employees", emp_id, {
        "employee_id": emp_id, "employer_ref": ctx.client_id, "forename": " ".join(parts[:-1]), "surname": parts[-1],
        "ni_number": v.get("ni_number"), "date_of_birth": v["date_of_birth"], "start_date": v["start_date"],
        "annual_salary": float(v["annual_salary"]), "starter_statement": v["starter_statement"],
        "status": "set up, awaiting first payroll run", "source_case": ctx.case["ref"]})
    return {"summary": f"Employee {emp_id} created for {v['employee_name']}"}


# ---- onboarding ------------------------------------------------------------------------------

def crm_create_client(ctx: Ctx) -> dict:
    v, data = ctx.values, ctx.case["data"]
    crm = systems.system("crm")
    if not data.get("new_client_id"):
        words = [w for w in re.findall(r"[A-Za-z]+", v["company_name"]) if w.lower() not in ("ltd", "limited", "llp")]
        initials = "".join(w[0] for w in words)[:3].upper()
        initials = (initials + (words[-1][1:] if words else "").upper() + "XXX")[:3]
        n = 1
        while crm.get("accounts", f"{initials}{n:03d}"):
            n += 1
        data["new_client_id"] = f"{initials}{n:03d}"
        ctx.case["client_id"] = data["new_client_id"]
        store.save_case(ctx.con, ctx.case)
        ctx.con.commit()
    cid = data["new_client_id"]
    ch = systems.companies_house_lookup(v["company_number"]) or {}
    sender = ctx.case["sender"]
    ctx.write("crm", "accounts", cid, {
        "account_id": cid, "name": v["company_name"], "type": "limited_company", "company_number": v["company_number"],
        "manager": config.DEFAULT_USER, "services": [s.strip() for s in re.split(r",| and ", v["services"]) if s.strip()],
        "payroll": "payroll" in v["services"].lower(), "pay_day": 25,
        "address": {"line1": "", "town": "", "postcode": ch.get("registered_postcode", "")},
        "email_domain": sender.partition("@")[2], "status": "onboarding", "source_case": ctx.case["ref"]})
    return {"summary": f"Client {cid} created in the CRM"}


def crm_add_contact(ctx: Ctx) -> dict:
    v = ctx.values
    cid = ctx.client_id
    ctx.write("crm", "contacts", f"CON-{cid}-1", {"contact_id": f"CON-{cid}-1", "account_id": cid,
                                                 "name": v["contact_name"], "email": ctx.case["sender"],
                                                 "role": "Director", "phone": ""})
    return {"summary": f"Contact {v['contact_name']} added"}


def dms_create_folder(ctx: Ctx) -> dict:
    cid = ctx.client_id
    ctx.write("dms", "folders", cid, {"client_id": cid, "path": f"Clients/{cid} {ctx.values.get('company_name', '')}"})
    return {"summary": f"Client folder created for {cid}"}


def accounting_create_contact(ctx: Ctx) -> dict:
    cid = ctx.client_id
    ctx.write("accounting", "contacts", f"acc-{cid.lower()}", {
        "ContactID": f"acc-{cid.lower()}", "AccountNumber": cid, "Name": ctx.values["company_name"], "Addresses": []})
    return {"summary": f"Accounting contact acc-{cid.lower()} created"}


def tax_create_client(ctx: Ctx) -> dict:
    cid = ctx.client_id
    ctx.write("tax", "clients", cid, {"client_code": cid, "name": ctx.values["company_name"], "entity": "company",
                                      "company_number": ctx.values["company_number"]})
    return {"summary": f"Tax client {cid} created"}


# ---- bank details -----------------------------------------------------------------------------

def crm_update_bank(ctx: Ctx) -> dict:
    v, acc = ctx.values, ctx.client()
    approvers = [a["user"] for a in ctx.case["data"].get("review", {}).get("approvals", [])]
    acc["bank"] = {"account_name": v["account_name"], "sort_code": v["sort_code"], "account_number": v["account_number"],
                   "verified_by": approvers, "verified_by_callback": True, "source_case": ctx.case["ref"]}
    ctx.write("crm", "accounts", ctx.client_id, acc)
    return {"summary": f"CRM bank details updated (account ending {v['account_number'][-4:]})"}


def accounting_update_bank(ctx: Ctx) -> dict:
    v = ctx.values
    contacts = systems.system("accounting").find("contacts", AccountNumber=ctx.client_id)
    if not contacts:
        return {"summary": "No accounting contact, nothing to change"}
    c = contacts[0]
    c["BankAccountDetails"] = f"{v['sort_code']} {v['account_number']}"
    ctx.write("accounting", "contacts", c["ContactID"], c)
    return {"summary": "Accounting contact bank details updated"}


# ---- records ------------------------------------------------------------------------------------

def dms_file_attachments(ctx: Ctx) -> dict:
    folder_for = {"new_starter_checklist": "Payroll", "identity_document": "Identity and AML",
                  "proof_of_address": "Identity and AML", "bank_statement": "Records", "sales_records": "Records"}
    period = ctx.values.get("period")
    filed = 0
    for d in store.case_documents(ctx.con, ctx.case["ref"]):
        if d["doc_type"] in (None, "unreadable"):
            continue
        folder = folder_for.get(d["doc_type"], "Correspondence")
        if folder == "Records" and period:
            folder = f"Records/{period}"
        target = config.sub("systems", "dms_files", ctx.client_id, *folder.split("/")) / f"{ctx.case['ref']}-{d['filename']}"
        if not target.exists():
            shutil.copyfile(d["path"], target)
        file_id = f"{ctx.client_id}-{ctx.case['ref']}-{d['id']}"
        ctx.write("dms", "files", file_id, {
            "file_id": file_id, "client_id": ctx.client_id, "folder": folder, "filename": d["filename"],
            "doc_type": d["doc_type"], "sha256": d["sha256"], "case_ref": ctx.case["ref"],
            "path": str(target.relative_to(config.data_dir()))}, key=f":{d['id']}")
        filed += 1
    ctx.case["data"]["files_filed"] = filed
    return {"summary": f"{filed} document(s) filed"}


def tax_open_return(ctx: Ctx) -> dict:
    period = ctx.values["period"]
    rid = f"{ctx.client_id}-SA-{period}"
    ctx.write("tax", "returns", rid, {"return_id": rid, "client_code": ctx.client_id, "tax_year": period,
                                      "status": "records received", "source_case": ctx.case["ref"]})
    return {"summary": f"Tax return {rid} opened"}


def accounting_open_job(ctx: Ctx) -> dict:
    period = ctx.values["period"]
    jid = f"{ctx.client_id}-ACCS-{period}"
    ctx.write("accounting", "jobs", jid, {"JobID": jid, "AccountNumber": ctx.client_id, "Type": "Year-end accounts",
                                          "Period": period, "Status": "records received"})
    return {"summary": f"Accounts job {jid} opened"}


# ---- tasks, documents and notes -------------------------------------------------------------------

def _owner(role: str, client: dict) -> str:
    return client.get("manager", config.DEFAULT_USER) if role == "manager" else config.ROLE_OWNERS.get(role, config.DEFAULT_USER)


def crm_create_tasks(ctx: Ctx) -> dict:
    cfg = CONFIG[ctx.case["instruction_type"]]
    v, client = dict(ctx.values), ctx.client()
    v.setdefault("client_name", client.get("name", ""))
    v["purpose_label"] = "tax return" if v.get("purpose") == "tax_return" else "accounts"
    created = []
    for n, (title, role, due_rule) in enumerate(cfg.tasks, 1):
        if due_rule == "payroll_cutoff":
            start = config.today()
            if v.get("start_date"):
                start = max(start, dt.date.fromisoformat(v["start_date"]))
            due = config.add_working_days(config.next_pay_date(client.get("pay_day", 25), start), -2)
        else:
            due = config.add_working_days(config.today(), int(due_rule))
        task_id = f"TASK-{ctx.case['ref']}-{n}"
        text = title.format(**{k: (val or "") for k, val in v.items()})
        ctx.write("crm", "tasks", task_id, {"task_id": task_id, "account_id": ctx.client_id, "title": text,
                                            "owner": _owner(role, client), "due": due.isoformat(), "status": "open",
                                            "case_ref": ctx.case["ref"]}, key=f":{n}")
        created.append(f"{text} ({config.USERS[_owner(role, client)]['name']}, due {config.uk_date(due)})")
    ctx.case["data"]["tasks"] = created
    return {"summary": f"{len(created)} task(s) created" if created else "No tasks needed"}


def dms_generate_documents(ctx: Ctx) -> dict:
    cfg = CONFIG[ctx.case["instruction_type"]]
    client = ctx.client()
    made = []
    for kind in cfg.documents:
        title, paragraphs = documents.letter_content(kind, ctx.case, ctx.values, client)
        target = config.sub("systems", "dms_files", ctx.client_id, "Letters") / f"{ctx.case['ref']}-{kind}.pdf"
        if not target.exists():
            documents.write_letter(target, title, paragraphs)
        file_id = f"{ctx.client_id}-{ctx.case['ref']}-{kind}"
        record = {"file_id": file_id, "client_id": ctx.client_id, "folder": "Letters", "filename": target.name,
                  "doc_type": kind, "case_ref": ctx.case["ref"], "path": str(target.relative_to(config.data_dir()))}
        if kind == "engagement_letter":
            record["signature_status"] = "sent for e-signature"
        ctx.write("dms", "files", file_id, record, key=f":{kind}")
        made.append(target.name)
    ctx.case["data"]["documents_generated"] = made
    return {"summary": f"Generated {', '.join(made)}" if made else "No documents needed"}


def crm_add_note(ctx: Ctx) -> dict:
    from .extraction import type_label
    note_id = f"NOTE-{ctx.case['ref']}"
    approvals = ctx.case["data"].get("review", {}).get("approvals", [])
    how = ("approved by " + ", ".join(config.USERS[a["user"]]["name"] for a in approvals)) if approvals \
        else "processed automatically"
    ctx.write("crm", "notes", note_id, {"note_id": note_id, "account_id": ctx.client_id, "case_ref": ctx.case["ref"],
                                        "text": f"{type_label(ctx.case['instruction_type'])} from email, {how}.",
                                        "at": config.now_iso()})
    return {"summary": "Note added to the client record"}


OPS = {
    "crm.update_address": crm_update_address, "accounting.update_address": accounting_update_address,
    "tax.update_address": tax_update_address, "payroll.update_employer_address": payroll_update_employer_address,
    "payroll.create_employee": payroll_create_employee, "crm.create_client": crm_create_client,
    "crm.add_contact": crm_add_contact, "dms.create_folder": dms_create_folder,
    "accounting.create_contact": accounting_create_contact, "tax.create_client": tax_create_client,
    "crm.update_bank": crm_update_bank, "accounting.update_bank": accounting_update_bank,
    "dms.file_attachments": dms_file_attachments, "tax.open_return": tax_open_return,
    "accounting.open_job": accounting_open_job, "crm.create_tasks": crm_create_tasks,
    "dms.generate_documents": dms_generate_documents, "crm.add_note": crm_add_note,
}
