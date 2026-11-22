"""Actions people take on a case: approve, correct, ask the client, reject, release, retry, cancel."""
from __future__ import annotations

from . import adapters, config, orchestrator, routing, store, systems
from .extraction import SCHEMAS


class ReviewError(Exception):
    """Shown to the reviewer as-is, so messages must be plain and say what to do."""


def _user(user_id: str) -> dict:
    if user_id not in config.USERS:
        raise ReviewError("Unknown user.")
    return config.USERS[user_id]


def _load(con, ref: str, allowed: set[str]) -> dict:
    case = store.get_case(con, ref)
    if not case:
        raise ReviewError(f"There is no case {ref}.")
    if case["status"] not in allowed:
        raise ReviewError(f"This case is {case['status'].replace('_', ' ')}, so that action is not available.")
    return case


def approve(ref: str, user_id: str, note: str = "", callback_confirmed: bool = False) -> str:
    user = _user(user_id)
    with store.session() as con:
        case = _load(con, ref, {"awaiting_review"})
        itype = case.get("instruction_type")
        if itype not in routing.CONFIG:
            raise ReviewError("Set the instruction type first (Correct a value, then choose instruction_type).")
        cfg = routing.CONFIG[itype]
        issues = case["data"].get("validation", {}).get("issues", [])
        if any(i["severity"] == "unknown_client" for i in issues):
            raise ReviewError("The sender is not a known contact for any client, so this cannot be approved. "
                              "Reject it, or contact the client on the details already on file.")
        blocking = [i["message"] for i in issues if i["severity"] in ("missing", "invalid", "unverified")]
        if blocking:
            raise ReviewError("Some values are missing or could not be checked against the source. "
                              "Correct them, or ask the client, before approving: " + " ".join(blocking))
        review = case["data"].setdefault("review", {"roles_needed": [], "approvals": [], "reasons": []})
        approvals = review["approvals"]
        if any(a["user"] == user_id for a in approvals):
            raise ReviewError("You have already approved this case. The next approval must come from someone else.")
        if cfg.four_eyes and any(c["user"] == user_id for c in case["data"].get("corrections", {}).values()):
            raise ReviewError("You changed values on this case, so a different person must approve it.")
        if cfg.callback_required and not callback_confirmed:
            raise ReviewError("Before approving a bank details change, call the client on the phone number already "
                              "on file, confirm the new details with them, and tick the box to say you have.")
        needed = review.get("roles_needed", [])
        covered = {a["role"] for a in approvals}
        outstanding = [r for r in needed if r not in covered]
        if needed:
            role = next((r for r in outstanding if r in user["roles"]), None)
            if role is None:
                raise ReviewError("This case needs approval from " +
                                  " and ".join(config.ROLE_LABELS[r] for r in outstanding) +
                                  ". Your role cannot give that approval.")
        else:
            if "reviewer" not in user["roles"]:
                raise ReviewError("Only reviewers can approve cases.")
            role = "reviewer"
        approvals.append({"user": user_id, "role": role, "at": config.now_iso(), "note": note,
                          "callback_confirmed": callback_confirmed, "data_hash": orchestrator.data_hash(case)})
        store.audit(con, ref, user_id, "approved", role=role, note=note, callback_confirmed=callback_confirmed,
                    data_hash=orchestrator.data_hash(case))
        remaining = [r for r in needed if r not in covered | {role}]
        if remaining:
            store.save_case(con, case)
            message = "Approved. It still needs approval from " + " and ".join(config.ROLE_LABELS[r] for r in remaining) + "."
        else:
            store.set_status(con, case, "approved", user_id, reason="Approved")
            message = "Approved. The system updates are running now."
    if not remaining:
        orchestrator.advance(ref)
    return message


def correct(ref: str, user_id: str, field: str, value: str, reason: str) -> str:
    _user(user_id)
    if not (reason or "").strip():
        raise ReviewError("Say why you are changing the value, for the audit trail.")
    with store.session() as con:
        case = _load(con, ref, {"awaiting_review", "awaiting_client"})
        if field == "instruction_type":
            if value not in SCHEMAS:
                raise ReviewError("Choose one of the listed instruction types.")
            before = case.get("instruction_type")
        else:
            itype = case.get("instruction_type")
            if itype not in SCHEMAS or not SCHEMAS[itype].get(field):
                raise ReviewError("That field is not part of this instruction type.")
            before = case["data"].get("validation", {}).get("fields", {}).get(field, {}).get("value")
        case["data"].setdefault("corrections", {})[field] = {"value": value.strip(), "user": user_id,
                                                             "reason": reason.strip(), "at": config.now_iso()}
        if "review" in case["data"]:
            case["data"]["review"]["approvals"] = []
        store.audit(con, ref, user_id, "value_corrected", field=field, before=before, after=value.strip(), reason=reason)
        store.set_status(con, case, "screened", user_id, reason="Re-checking after a correction")
    orchestrator.advance(ref)
    return "Value corrected and the case re-checked. Any earlier approvals have been cleared."


def ask_client(ref: str, user_id: str, fields: list[str], note: str = "") -> str:
    _user(user_id)
    with store.session() as con:
        case = _load(con, ref, {"awaiting_review"})
        orchestrator.request_info(con, case, fields, user_id, note or None)
    return "Request sent to the client."


def reject(ref: str, user_id: str, reason: str) -> str:
    _user(user_id)
    if not (reason or "").strip():
        raise ReviewError("Give a reason for rejecting the case.")
    with store.session() as con:
        case = _load(con, ref, {"awaiting_review", "held_security", "awaiting_client", "exception"})
        if con.execute("SELECT 1 FROM writes WHERE case_ref=? AND undone=0", (ref,)).fetchone():
            raise ReviewError("This case has already changed some records. Cancel it instead, which undoes them.")
        store.audit(con, ref, user_id, "rejected", reason=reason)
        store.set_status(con, case, "rejected", user_id, reason=reason)
    return "Case rejected. Nothing was changed in any system."


def release(ref: str, user_id: str, note: str) -> str:
    user = _user(user_id)
    if "security" not in user["roles"]:
        raise ReviewError("Only a client manager can release a case held for a security check.")
    if not (note or "").strip():
        raise ReviewError("Record how you checked the sender (for example, a call to the number on file).")
    with store.session() as con:
        case = _load(con, ref, {"held_security"})
        case["data"].setdefault("screening", {})["released_by"] = user_id
        store.audit(con, ref, user_id, "security_hold_released", note=note)
        store.set_status(con, case, "screened", user_id, reason="Released after a security check")
    orchestrator.advance(ref)
    return "Released. The case is being processed."


def retry(ref: str, user_id: str) -> str:
    _user(user_id)
    with store.session() as con:
        case = _load(con, ref, {"exception"})
        failed = case["data"].pop("exception", {})
        if failed.get("system"):
            orchestrator.breaker_reset(failed["system"])
        con.execute("UPDATE steps SET status='pending', attempts=0, error=NULL WHERE case_ref=? AND status='failed'", (ref,))
        store.audit(con, ref, user_id, "retry_requested", failed_step=failed.get("op"))
        store.set_status(con, case, "approved", user_id, reason="Retrying from the step that failed")
    orchestrator.advance(ref)
    return "Retrying from the step that failed."


def cancel(ref: str, user_id: str, reason: str) -> str:
    _user(user_id)
    if not (reason or "").strip():
        raise ReviewError("Give a reason for cancelling the case.")
    with store.session() as con:
        case = _load(con, ref, {"exception", "awaiting_signature"})
        undone = adapters.undo_case(con, ref, user_id)
        store.audit(con, ref, user_id, "cancelled", reason=reason, changes_undone=len(undone))
        store.set_status(con, case, "cancelled", user_id, reason=reason)
    return f"Case cancelled and {len(undone)} change(s) undone."


def signature_received(ref: str, actor: str = "esign-webhook") -> str:
    with store.session() as con:
        case = _load(con, ref, {"awaiting_signature"})
        file_id = f"{case['client_id']}-{ref}-engagement_letter"
        dms = systems.system("dms")
        record = dms.get("files", file_id) or {}
        record.update(signature_status="signed", signed_at=config.now_iso())
        adapters.write_once(con, ref, f"{ref}:signature", "dms", "files", file_id, record)
        store.audit(con, ref, actor, "signature_received", document=file_id)
        from . import documents
        documents.send_email(con, case, "confirmation", orchestrator.values(case))
        store.set_status(con, case, "completed", actor, reason="Engagement letter signed")
    return "Signature recorded and the case completed."
