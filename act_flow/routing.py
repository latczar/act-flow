"""Routing configuration and the routing decision.

Each instruction type lists the approvals it needs, the system updates it makes (in
order), the tasks it creates and the documents it produces. Adding an instruction
type means adding a schema, an entry here and tests, not new workflow code.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .extraction import SCHEMAS


@dataclass(frozen=True)
class Step:
    system: str
    op: str
    when: str | None = None      # optional condition: "field=value"

    def applies(self, values: dict) -> bool:
        if not self.when:
            return True
        name, _, expected = self.when.partition("=")
        return values.get(name) == expected


@dataclass(frozen=True)
class TypeConfig:
    approval_roles: tuple[str, ...] = ()
    callback_required: bool = False
    four_eyes: bool = False                 # whoever corrects values cannot approve
    auto_threshold: float = 0.85
    steps: tuple[Step, ...] = ()
    tasks: tuple[tuple[str, str, object], ...] = ()    # (title template, owner role, due: working days or rule)
    documents: tuple[str, ...] = ()
    needs_signature: bool = False


FINISH = (Step("crm", "create_tasks"), Step("dms", "generate_documents"), Step("crm", "add_note"))

CONFIG: dict[str, TypeConfig] = {
    "client_details_change": TypeConfig(
        steps=(Step("crm", "update_address"), Step("accounting", "update_address"),
               Step("tax", "update_address"), Step("payroll", "update_employer_address"), *FINISH)),
    "payroll_new_starter": TypeConfig(
        approval_roles=("payroll",),
        steps=(Step("payroll", "create_employee"), Step("dms", "file_attachments"), *FINISH),
        tasks=(("Check {employee_name} is set up before the next payroll run", "payroll_lead", "payroll_cutoff"),)),
    "new_client_onboarding": TypeConfig(
        approval_roles=("compliance",),
        steps=(Step("crm", "create_client"), Step("crm", "add_contact"), Step("dms", "create_folder"),
               Step("dms", "file_attachments"), Step("accounting", "create_contact"),
               Step("tax", "create_client"), *FINISH),
        tasks=(("Record the identity and AML check outcome for {company_name}", "compliance", 2),
               ("Set up the engagement and services for {company_name}", "manager", 5)),
        documents=("engagement_letter", "welcome_letter"),
        needs_signature=True),
    "bank_details_change": TypeConfig(
        approval_roles=("manager", "finance"), callback_required=True, four_eyes=True,
        steps=(Step("crm", "update_bank"), Step("accounting", "update_bank"), *FINISH)),
    "records_submission": TypeConfig(
        steps=(Step("dms", "file_attachments"),
               Step("tax", "open_return", when="purpose=tax_return"),
               Step("accounting", "open_job", when="purpose=accounts"), *FINISH),
        tasks=(("Prepare the {period} {purpose_label} for {client_name}", "manager", 10),)),
}


def decide(case: dict, screening: dict, extraction: dict, validation: dict) -> dict:
    """Choose the route: held_security, review, awaiting_client or auto."""
    if screening.get("status") == "suspicious" and not screening.get("released_by"):
        return {"route": "held_security", "reasons": screening.get("flags", []), "roles_needed": ["security"]}

    itype = extraction.get("instruction_type")
    if itype not in SCHEMAS:
        return {"route": "review", "roles_needed": [],
                "reasons": ["The system could not tell what this email is asking for, so a person needs to triage it."]}
    cfg = CONFIG[itype]
    issues = validation.get("issues", [])

    if not screening.get("known_sender") and itype != "new_client_onboarding":
        return {"route": "review", "roles_needed": ["security"],
                "reasons": ["The sender is not a known contact for any client."]}

    client_fixable = [i for i in issues if i["severity"] in ("missing", "invalid") and i["client_can_fix"]]
    if client_fixable:
        return {"route": "awaiting_client", "roles_needed": [], "missing": [i["field"] for i in client_fixable],
                "reasons": [i["message"] for i in client_fixable]}

    reasons = [i["message"] for i in issues]
    roles = list(cfg.approval_roles)
    if roles:
        reasons.append(f"{SCHEMAS[itype].label} always needs approval ({', '.join(roles)}).")
    if validation.get("confidence", 0) < cfg.auto_threshold:
        reasons.append(f"Confidence {validation.get('confidence', 0):.0%} is below the {cfg.auto_threshold:.0%} "
                       "needed to go ahead automatically.")
    if reasons:
        return {"route": "review", "roles_needed": roles, "reasons": reasons}
    return {"route": "auto", "roles_needed": [], "reasons": ["All checks passed and this is a low-risk instruction type."]}
