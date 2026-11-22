"""Email intake: pick up messages, keep the originals unaltered, screen senders and read documents."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path

from . import config, store, systems
from .extraction import Source

REF_RE = re.compile(r"\b(ACT-\d{4}-\d{6})\b")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "attachment")[:80]


def _make_read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD)


def parse_auth_results(header: str | None) -> dict:
    out = {"spf": "none", "dkim": "none", "dmarc": "none"}
    for key in out:
        m = re.search(rf"\b{key}=(\w+)", header or "", re.I)
        if m:
            out[key] = m.group(1).lower()
    return out


def body_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    return text


def clean_body(text: str) -> str:
    """Remove quoted earlier messages, signatures after '-- ' and standard disclaimers."""
    text = text.replace("\r\n", "\n")
    text = re.split(r"^On .{5,120}wrote:\s*$", text, maxsplit=1, flags=re.M)[0]
    text = re.split(r"^-- ?$", text, maxsplit=1, flags=re.M)[0]
    text = re.split(r"^(?:This email and any attachments|DISCLAIMER)", text, maxsplit=1, flags=re.M | re.I)[0]
    lines = [ln for ln in text.split("\n") if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def match_case(con, msg) -> str | None:
    """Replies join their case by reference in the subject, or by the email thread."""
    m = REF_RE.search(str(msg.get("Subject", "")))
    if m and store.get_case(con, m.group(1)):
        return m.group(1)
    ids = re.findall(r"<[^>]+>", f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}")
    for mid in ids:
        row = con.execute("SELECT case_ref FROM messages WHERE message_id=?", (mid,)).fetchone()
        if row:
            return row["case_ref"]
    return None


def ingest_inbox() -> list[str]:
    """Move every .eml in the inbox into evidence storage and attach it to a case."""
    inbox = config.sub("inbox")
    touched: list[str] = []
    for path in sorted(inbox.glob("*.eml")):
        raw = path.read_bytes()
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        message_id = str(msg.get("Message-ID") or f"<{sha256(raw)[:24]}@generated.{config.ACT_DOMAIN}>").strip()
        display, address = parseaddr(str(msg.get("From", "")))
        subject = str(msg.get("Subject", "")).strip()
        with store.session() as con:
            if con.execute("SELECT 1 FROM messages WHERE message_id=?", (message_id,)).fetchone():
                store.audit(con, None, "system", "duplicate_email_ignored", message_id=message_id, sender=address)
                dup_dir = config.sub("inbox", "duplicates")
                shutil.move(str(path), dup_dir / f"{sha256(raw)[:12]}-{path.name}")
                continue
            ref = match_case(con, msg)
            case = store.get_case(con, ref) if ref else None
            if case and case["status"] in ("completed", "rejected", "cancelled"):
                related, case = case["ref"], None
            else:
                related = None
            if case is None:
                case = store.create_case(con, address.lower(), subject)
                if related:
                    case["data"]["related_case"] = related
            n = len(store.case_messages(con, case["ref"])) + 1
            evidence = config.sub("evidence", case["ref"])
            eml_path = evidence / f"{n:02d}-message.eml"
            eml_path.write_bytes(raw)
            _make_read_only(eml_path)
            headers = {"from": address.lower(), "display_name": display, "subject": subject,
                       "date": str(msg.get("Date", "")), "auth": parse_auth_results(msg.get("Authentication-Results")),
                       "in_reply_to": str(msg.get("In-Reply-To", "")), "body": clean_body(body_text(msg))}
            con.execute("INSERT INTO messages(message_id, case_ref, direction, path, received_at, sha256, headers) "
                        "VALUES(?,?,?,?,?,?,?)", (message_id, case["ref"], "in", str(eml_path), config.now_iso(),
                                                  sha256(raw), json.dumps(headers)))
            files = []
            for part in msg.iter_attachments():
                content = part.get_payload(decode=True) or b""
                name = _safe_name(part.get_filename())
                file_path = evidence / f"{n:02d}-{name}"
                file_path.write_bytes(content)
                _make_read_only(file_path)
                con.execute("INSERT INTO documents(case_ref, message_id, filename, path, sha256, size, received_at) "
                            "VALUES(?,?,?,?,?,?,?)", (case["ref"], message_id, name, str(file_path), sha256(content),
                                                      len(content), config.now_iso()))
                files.append({"name": name, "sha256": sha256(content), "bytes": len(content)})
            store.audit(con, case["ref"], "system", "email_received", message_id=message_id, sender=address,
                        subject=subject, sha256=sha256(raw), attachments=files, reply=bool(ref and n > 1))
            if n > 1:
                case["data"]["needs_extraction"] = True
                case["data"].pop("review", None)
                if case["status"] != "received":
                    store.set_status(con, case, "received", reason="New email received on this case")
                else:
                    store.save_case(con, case)
            else:
                case["data"]["needs_extraction"] = True
                store.save_case(con, case)
            path.unlink()
            touched.append(case["ref"])
    return touched


# ---- screening ---------------------------------------------------------------------------

def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def screen(con, case: dict) -> dict:
    """Check the latest email's sender and authentication, and the attachments."""
    inbound = store.case_messages(con, case["ref"], "in")
    latest = inbound[-1]["headers"]
    sender, domain = latest["from"], latest["from"].partition("@")[2]
    crm = systems.system("crm")
    contacts = crm.find("contacts", email=sender)
    contact = contacts[0] if contacts else None
    flags, status = [], "clean"
    auth = latest["auth"]
    if auth.get("dmarc") != "pass":
        flags.append(f"Sender authentication failed (SPF {auth.get('spf')}, DKIM {auth.get('dkim')}, "
                     f"DMARC {auth.get('dmarc')}).")
    if not contact:
        all_contacts = crm.find("contacts")
        known_domains = {c["email"].partition("@")[2] for c in all_contacts}
        lookalike = [d for d in known_domains if 0 < _distance(domain, d) <= 2]
        if lookalike:
            flags.append(f"The sender's domain {domain} looks like a client's domain ({lookalike[0]}).")
        if any(c["name"].lower() == (latest.get("display_name") or "").lower() for c in all_contacts):
            flags.append("The display name matches a known contact but the email address does not.")
    if case.get("client_id") and contact and contact["account_id"] != case["client_id"]:
        flags.append("This reply came from a different client than the original email.")
    if flags:
        status = "suspicious"
    rejected = []
    for d in store.case_documents(con, case["ref"]):
        ext = Path(d["filename"]).suffix.lower()
        if ext not in config.ALLOWED_EXTENSIONS:
            rejected.append(f"{d['filename']}: file type not accepted")
        elif d["size"] > config.MAX_ATTACHMENT_BYTES:
            rejected.append(f"{d['filename']}: larger than 10 MB")
    return {"status": status, "flags": flags, "known_sender": bool(contact),
            "client_id": contact["account_id"] if contact else None,
            "contact": contact["name"] if contact else None, "auth": auth, "rejected_files": rejected,
            "released_by": None}


# ---- reading documents -----------------------------------------------------------------------

DOC_TYPES = [
    ("new_starter_checklist", r"starter checklist"),
    ("identity_document", r"passport|driving licence"),
    ("proof_of_address", r"utility bill|council tax|proof of address"),
    ("bank_statement", r"bank statement|statement of account"),
    ("sales_records", r"sales|invoice list"),
]


def read_document(path: str, filename: str) -> list[str] | None:
    ext = Path(filename).suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        return None
    if ext == ".pdf":
        from pypdf import PdfReader
        try:
            reader = PdfReader(path)
            if reader.is_encrypted:
                return None
            return [(p.extract_text() or "").strip() for p in reader.pages]
        except Exception:
            return None
    return [Path(path).read_text(encoding="utf-8", errors="replace")]


def classify_document(filename: str, pages: list[str] | None) -> str:
    if pages is None:
        return "unreadable"
    haystack = f"{filename}\n" + "\n".join(pages)[:3000]
    for doc_type, pattern in DOC_TYPES:
        if re.search(pattern, haystack, re.I):
            return doc_type
    return "other"


def load_sources(con, case: dict) -> list[Source]:
    """Email bodies (original first, then replies) and the text of each readable attachment."""
    sources = []
    for i, m in enumerate(store.case_messages(con, case["ref"], "in")):
        h = m["headers"]
        text = f"From: {h['display_name']} <{h['from']}>\nSubject: {h['subject']}\n\n{h['body']}"
        sources.append(Source("email" if i == 0 else f"reply{i}", "email" if i == 0 else "reply", text))
    for n, d in enumerate(store.case_documents(con, case["ref"]), 1):
        if d["doc_type"] is None:
            pages = read_document(d["path"], d["filename"])
            d["doc_type"] = classify_document(d["filename"], pages)
            d["pages"] = pages
            con.execute("UPDATE documents SET doc_type=?, pages=? WHERE id=?",
                        (d["doc_type"], json.dumps(pages) if pages is not None else None, d["id"]))
            store.audit(con, case["ref"], "system", "document_read", document=d["filename"], doc_type=d["doc_type"],
                        pages=len(pages or []))
        if d["pages"]:
            sources.append(Source(f"doc{n}", "doc", "", pages=d["pages"], filename=d["filename"], doc_type=d["doc_type"]))
    return sources
