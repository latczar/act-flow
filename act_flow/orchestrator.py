"""The workflow engine. Moves each case through its stages and runs system updates safely.

Stages: received -> screened -> (awaiting_client | awaiting_review | held_security | approved)
        approved -> system updates -> (awaiting_signature | completed), or exception on failure.
Every transition and every system call is written to the audit log.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import random
import time

from . import adapters, config, documents, intake, routing, store, systems, validation
from .extraction import SCHEMAS, ExtractionError, get_extractor

log = logging.getLogger("act_flow")

WAITING = {"awaiting_client", "awaiting_review", "held_security", "awaiting_signature", "exception"}
TERMINAL = {"completed", "rejected", "cancelled"}
ACTIVE = ("received", "screened", "approved")

_breakers: dict[str, dict] = {}


def breaker_open(name: str) -> bool:
    b = _breakers.get(name)
    return bool(b and b["open_until"] > time.time())


def breaker_record(name: str, ok: bool) -> None:
    b = _breakers.setdefault(name, {"failures": 0, "open_until": 0.0})
    if ok:
        b.update(failures=0, open_until=0.0)
    else:
        b["failures"] += 1
        if b["failures"] >= config.BREAKER_THRESHOLD:
            b["open_until"] = time.time() + config.BREAKER_SECONDS


def breaker_reset(name: str) -> None:
    _breakers.pop(name, None)


def values(case: dict) -> dict:
    return {k: e["value"] for k, e in case["data"].get("validation", {}).get("fields", {}).items()}


def data_hash(case: dict) -> str:
    """Approvals are tied to the exact values approved."""
    payload = store.dumps({"type": case.get("instruction_type"), "values": values(case)})
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---- entry points -------------------------------------------------------------------------

def process_all(extractor=None) -> list[str]:
    intake.ingest_inbox()
    with store.session() as con:
        check_reminders(con)
        refs = [r["ref"] for r in con.execute(
            f"SELECT ref FROM cases WHERE status IN ({','.join('?' * len(ACTIVE))}) ORDER BY ref", ACTIVE)]
    for ref in refs:
        advance(ref, extractor)
    return refs


def advance(ref: str, extractor=None) -> None:
    """Run the case forward until it finishes or has to wait for someone."""
    with store.session() as con:
        for _ in range(12):
            case = store.get_case(con, ref)
            status = case["status"]
            try:
                if status == "received":
                    _screen(con, case)
                elif status == "screened":
                    _assess(con, case, extractor)
                elif status == "approved":
                    _execute(con, case)
                else:
                    break
            except systems.TransientError as e:
                con.rollback()
                store.audit(con, ref, "system", "will_retry_later", stage=status, error=str(e))
                log.warning("%s: %s; will retry later", ref, e)
                break
            except ExtractionError as e:
                con.rollback()
                if e.transient:
                    store.audit(con, ref, "system", "will_retry_later", stage="extraction", error=str(e))
                    break
                case = store.get_case(con, ref)
                case["data"]["needs_extraction"] = False
                case["data"].setdefault("extraction", {"instruction_type": "unknown", "fields": {}, "extractor": "failed"})
                case["data"]["review"] = {"roles_needed": [], "approvals": [], "reasons": [str(e)]}
                store.audit(con, ref, "system", "extraction_failed", error=str(e))
                store.set_status(con, case, "awaiting_review", reason=str(e))
            con.commit()


# ---- stages ----------------------------------------------------------------------------------

def _screen(con, case: dict) -> None:
    res = intake.screen(con, case)
    data = case["data"]
    data["screening"] = res
    if res["client_id"] and not case.get("client_id"):
        case["client_id"] = res["client_id"]
    store.audit(con, case["ref"], "system", "screened", result=res["status"], flags=res["flags"],
                known_sender=res["known_sender"], client=res["client_id"], rejected_files=res["rejected_files"])
    if res["status"] == "suspicious":
        data["route"] = {"route": "held_security", "reasons": res["flags"], "roles_needed": ["security"]}
        store.set_status(con, case, "held_security", reason="Held for a security check. " + res["flags"][0])
        return
    if res["known_sender"] and not data.get("ack_sent"):
        documents.send_email(con, case, "acknowledgement")
        data["ack_sent"] = True
    store.set_status(con, case, "screened")


def _assess(con, case: dict, extractor=None) -> None:
    data = case["data"]
    sources = intake.load_sources(con, case)
    if data.get("needs_extraction", True) or "extraction" not in data:
        ex = extractor or get_extractor()
        result = ex.extract(sources)
        data["extraction"], data["needs_extraction"] = result, False
        store.audit(con, case["ref"], ex.name, "extracted", instruction_type=result["instruction_type"],
                    model=result.get("model"), prompt_version=result.get("prompt_version"),
                    schema_version=result.get("schema_version"), other_requests=result.get("other_requests"),
                    fields={k: {"value": f["value"], "source": f["source"], "confidence": f["confidence"]}
                            for k, f in result["fields"].items()})

    extraction = {**data["extraction"], "fields": dict(data["extraction"].get("fields", {}))}
    corrections = dict(data.get("corrections", {}))
    if "instruction_type" in corrections:
        extraction["instruction_type"] = corrections.pop("instruction_type")["value"]
    itype = extraction.get("instruction_type")
    case["instruction_type"] = itype if itype in SCHEMAS else None

    if itype in SCHEMAS and SCHEMAS[itype].get("client_name") and case.get("client_id") \
            and not extraction["fields"].get("client_name"):
        client = systems.system("crm").get("accounts", case["client_id"])
        if client:
            extraction["fields"]["client_name"] = {"value": client["name"], "source": "crm_match",
                                                   "quote": None, "confidence": 0.99}

    val = validation.validate(con, case, extraction, sources, corrections)
    data["validation"] = val
    store.audit(con, case["ref"], "system", "validated", confidence=val["confidence"],
                issues=[i["message"] for i in val["issues"]],
                checks={c["check"]: c["result"] for c in val["checks"]})

    decision = routing.decide(case, data.get("screening", {}), extraction, val)
    data["route"] = decision
    store.audit(con, case["ref"], "system", "routed", route=decision["route"], reasons=decision["reasons"])

    screening = data.get("screening", {})
    if itype == "new_client_onboarding" and not data.get("ack_sent") \
            and screening.get("auth", {}).get("dmarc") == "pass" and decision["route"] != "held_security":
        documents.send_email(con, case, "acknowledgement")
        data["ack_sent"] = True

    route = decision["route"]
    if route == "held_security":
        store.set_status(con, case, "held_security", reason="Held for a security check. " + decision["reasons"][0])
    elif route == "awaiting_client":
        request_info(con, case, decision["missing"], "system")
    elif route == "review":
        data["review"] = {"roles_needed": decision["roles_needed"], "reasons": decision["reasons"], "approvals": []}
        store.set_status(con, case, "awaiting_review", reason=decision["reasons"][0])
    else:
        data["straight_through"] = True
        store.set_status(con, case, "approved", reason="Straight through: all checks passed")


def request_info(con, case: dict, fields: list, actor: str, note: str | None = None) -> None:
    items = [documents.field_prompt(case.get("instruction_type"), f) for f in fields] or ["Further details"]
    documents.send_email(con, case, "request_info", values(case), {"items": items, "note": note})
    case["data"].setdefault("info_requests", []).append(
        {"at": config.today().isoformat(), "fields": fields, "by": actor, "reminders": 0})
    store.set_status(con, case, "awaiting_client", actor, reason="Waiting for the client: " + "; ".join(items))


def _execute(con, case: dict) -> None:
    ref, data = case["ref"], case["data"]
    cfg = routing.CONFIG[case["instruction_type"]]
    approvals = data.get("review", {}).get("approvals", [])
    if any(a["data_hash"] != data_hash(case) for a in approvals):
        data["review"]["approvals"] = []
        store.set_status(con, case, "awaiting_review", reason="The values changed after approval, so they need approving again.")
        return

    steps = store.case_steps(con, ref)
    if not steps:
        plan = [s for s in cfg.steps if s.applies(values(case))]
        for seq, s in enumerate(plan, 1):
            op = f"{s.system}.{s.op}"
            con.execute("INSERT INTO steps(case_ref, seq, system, op, status, attempts, idem_key) "
                        "VALUES(?,?,?,?,'pending',0,?)", (ref, seq, s.system, op, f"{ref}:{seq}:{op}"))
        store.audit(con, ref, "system", "update_plan", steps=[f"{s.system}.{s.op}" for s in plan])
        con.commit()
        steps = store.case_steps(con, ref)

    for step in steps:
        if step["status"] != "done" and not _run_step(con, case, step):
            return

    if cfg.needs_signature:
        documents.send_email(con, case, "signature_request", values(case))
        store.set_status(con, case, "awaiting_signature", reason="Engagement letter sent for e-signature")
    else:
        documents.send_email(con, case, "confirmation", values(case), {"files": data.get("files_filed", 0)})
        store.set_status(con, case, "completed", reason="All systems updated and the client told")


def _run_step(con, case: dict, step: dict) -> bool:
    ref, sysname = case["ref"], step["system"]
    label = systems.LABELS[sysname]
    if breaker_open(sysname):
        return _step_failed(con, case, step, "system_unavailable",
                            f"{label} has been failing, so calls to it are paused for a minute. "
                            "Retry the case once it is back; it will carry on from this step.")
    fn = adapters.OPS[step["op"]]
    attempts = step["attempts"] or 0
    for attempt in range(1, config.MAX_ATTEMPTS + 1):
        attempts += 1
        try:
            result = fn(adapters.Ctx(con, case, step["idem_key"]))
        except systems.TransientError as e:
            breaker_record(sysname, False)
            con.execute("UPDATE steps SET attempts=?, error=? WHERE case_ref=? AND seq=?", (attempts, str(e), ref, step["seq"]))
            store.audit(con, ref, "system", "system_call_failed", system=sysname, op=step["op"], attempt=attempt, error=str(e))
            con.commit()
            if attempt < config.MAX_ATTEMPTS:
                time.sleep(config.retry_delay() * 2 ** (attempt - 1) * (1 + random.random() * 0.2))
                continue
            return _step_failed(con, case, step, "system_unavailable",
                                f"{label} did not respond after {config.MAX_ATTEMPTS} attempts. Nothing is lost: "
                                "retry the case when it is back and it will carry on from this step.")
        except systems.PermanentError as e:
            return _step_failed(con, case, step, "rejected",
                                f"{label} rejected the update ({e}). Check the values, then retry or cancel the case.")
        breaker_record(sysname, True)
        con.execute("UPDATE steps SET status='done', attempts=?, result=?, error=NULL WHERE case_ref=? AND seq=?",
                    (attempts, store.dumps(result), ref, step["seq"]))
        store.save_case(con, case)
        store.audit(con, ref, "system", "system_updated", system=sysname, op=step["op"], summary=result.get("summary"))
        con.commit()
        return True
    return False


def _step_failed(con, case: dict, step: dict, kind: str, message: str) -> bool:
    con.execute("UPDATE steps SET status='failed', error=? WHERE case_ref=? AND seq=?", (message, case["ref"], step["seq"]))
    case["data"]["exception"] = {"kind": kind, "system": step["system"], "op": step["op"], "message": message,
                                 "at": config.now_iso()}
    store.set_status(con, case, "exception", reason=message)
    con.commit()
    return False


# ---- reminders ----------------------------------------------------------------------------------

def check_reminders(con) -> None:
    today = config.today()
    for row in con.execute("SELECT ref FROM cases WHERE status='awaiting_client'").fetchall():
        case = store.get_case(con, row["ref"])
        requests = case["data"].get("info_requests") or []
        if not requests:
            continue
        req = requests[-1]
        elapsed = config.working_days_between(dt.date.fromisoformat(req["at"]), today)
        items = [documents.field_prompt(case.get("instruction_type"), f) for f in req["fields"]] or ["Further details"]
        if (req["reminders"] == 0 and elapsed >= 3) or (req["reminders"] == 1 and elapsed >= 7):
            documents.send_email(con, case, "reminder", values(case), {"items": items})
            req["reminders"] += 1
            store.save_case(con, case)
        elif req["reminders"] == 2 and elapsed >= 10 and not req.get("escalated"):
            client = systems.system("crm").get("accounts", case["client_id"]) or {} if case.get("client_id") else {}
            owner = client.get("manager", config.DEFAULT_USER)
            adapters.write_once(con, case["ref"], f"{case['ref']}:escalation:{len(requests)}", "crm", "tasks",
                                f"TASK-{case['ref']}-chase", {
                                    "task_id": f"TASK-{case['ref']}-chase", "account_id": case.get("client_id"),
                                    "title": f"Client has not replied to a request for information ({case['ref']})",
                                    "owner": owner, "due": config.add_working_days(today, 1).isoformat(),
                                    "status": "open", "case_ref": case["ref"]})
            req["escalated"] = True
            store.save_case(con, case)
            store.audit(con, case["ref"], "system", "escalated", to=owner, working_days_waiting=elapsed)
