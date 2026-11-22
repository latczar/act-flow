"""ACT instruction automation: review console (Streamlit).

Start it with:  python -m act_flow serve   (from the project folder, using the project's .venv)
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import re
import threading
from pathlib import Path

import pandas as pd
import streamlit as st

from act_flow import config, demo, orchestrator, review, samples, store, systems
from act_flow.extraction import SCHEMAS, type_label

st.set_page_config(page_title="ACT instruction desk", page_icon=":material/menu_book:", layout="wide")

STATUS = {
    "completed": ("Completed", "green"), "approved": ("Approved", "blue"), "received": ("Received", "gray"),
    "screened": ("Screened", "gray"), "awaiting_review": ("Awaiting review", "orange"),
    "awaiting_client": ("Awaiting client", "violet"), "awaiting_signature": ("Awaiting signature", "violet"),
    "held_security": ("Held: security", "red"), "exception": ("Exception", "red"),
    "rejected": ("Rejected", "gray"), "cancelled": ("Cancelled", "gray"),
}
NEEDS_ACTION = ("awaiting_review", "held_security", "exception", "awaiting_signature")

# The theme (colours and fonts) is in .streamlit/config.toml. This adds the ledger details on top.
STYLE = """<style>
:root {
  --paper: #F6F2E8; --card: #FBF9F3; --ink: #1C2721; --muted: #6A6656; --rule: #D3C9B2;
  --green: #1F4D3A; --brass: #B08A3E; --red: #A3402F; --orange: #A8691A; --violet: #5D4E8C;
  --blue: #2C5878; --ok: #2E6B4F;
  --serif: 'Newsreader', Georgia, serif; --sans: 'Libre Franklin', system-ui, sans-serif;
  --mono: 'IBM Plex Mono', ui-monospace, monospace;
}
[data-testid="stDecoration"] { display: none; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stMainBlockContainer"] { padding-top: 2.4rem; max-width: 1360px; }

/* masthead */
.mast { display: flex; justify-content: space-between; align-items: flex-end; gap: .6rem 1.5rem; flex-wrap: wrap;
        border-bottom: 3px double var(--ink); padding-bottom: .6rem; margin-bottom: 1.1rem; }
.mast .kicker { font: 600 .68rem/1 var(--sans); letter-spacing: .16em; text-transform: uppercase; color: var(--muted);
                margin-bottom: .5rem; }
.mast .title { font: 500 2.4rem/1.05 var(--serif); letter-spacing: -.01em; color: var(--ink); }
.mast .dateline { font: italic 400 1.02rem/1.3 var(--serif); color: var(--muted); }

/* summary strip */
.strip-wrap { container-type: inline-size; }
.strip { display: grid; grid-template-columns: repeat(6, 1fr); border-bottom: 1px solid var(--rule); margin-bottom: 1.6rem; }
.strip > div { padding: .35rem 1rem .9rem; border-left: 1px solid var(--rule); }
.strip > div:first-child { border-left: none; padding-left: 0; }
/* narrower: three, then two, per row; the first cell of each row has no rule or indent */
@container (max-width: 820px) {
  .strip { grid-template-columns: repeat(3, 1fr); }
  .strip > div:nth-child(n) { border-left: 1px solid var(--rule); padding-left: 1rem; }
  .strip > div:nth-child(3n+1) { border-left: none; padding-left: 0; }
}
@container (max-width: 460px) {
  .strip { grid-template-columns: repeat(2, 1fr); }
  .strip > div:nth-child(n) { border-left: 1px solid var(--rule); padding-left: 1rem; }
  .strip > div:nth-child(odd) { border-left: none; padding-left: 0; }
}
.strip .lbl { font: 600 .64rem/1.25 var(--sans); letter-spacing: .14em; text-transform: uppercase; color: var(--muted); }
.strip .num { font: 500 2.15rem/1.1 var(--serif); font-variant-numeric: tabular-nums lining-nums; color: var(--ink);
              margin-top: .3rem; }
.strip .num small { font: 400 .82rem var(--sans); color: var(--muted); margin-left: .35rem; }
.strip .flag .num { color: var(--red); }

/* section labels: small capitals with a rule running off to the right */
.section-label { display: flex; align-items: center; gap: .75rem; font: 600 .66rem/1 var(--sans); letter-spacing: .16em;
                 text-transform: uppercase; color: var(--muted); margin: .4rem 0 .5rem; }
.section-label::after { content: ""; flex: 1; border-top: 1px solid var(--rule); }

/* the register */
.register-wrap { overflow-x: auto; margin-bottom: .4rem; }
.register { width: 100%; border-collapse: collapse; font: 400 .88rem/1.4 var(--sans); color: var(--ink); }
.register th { font: 600 .62rem/1 var(--sans); letter-spacing: .14em; text-transform: uppercase; color: var(--muted);
               text-align: left; padding: .5rem .7rem .55rem; border-bottom: 1.5px solid var(--ink); white-space: nowrap; }
.register td { padding: .62rem .7rem; border-bottom: 1px solid var(--rule); vertical-align: top; }
.register th:first-child, .register td:first-child { padding-left: .2rem; }
.register tr.open td { background: rgba(176, 138, 62, .12); }
.register tr.open td:first-child { box-shadow: inset 3px 0 0 var(--brass); padding-left: .6rem; }
.register .ref { font: 500 .8rem/1.6 var(--mono); color: var(--green); white-space: nowrap; }
.register .from { color: var(--muted); font-size: .82rem; }
.register .why { font-size: .84rem; max-width: 46ch; }
/* when the register itself is narrow (a phone, or a small window with the sidebar open), each case becomes a
   short stacked entry: reference and status on one line, then the instruction, sender and where it stands */
.register-wrap { container-type: inline-size; }
@container (max-width: 640px) {
  .register thead { display: none; }
  .register, .register tbody { display: block; }
  .register tr { display: grid; grid-template-columns: 1fr auto; gap: .2rem .8rem; align-items: center;
                 padding: .75rem .2rem .8rem; border-bottom: 1px solid var(--rule); }
  .register td, .register td:first-child { display: block; padding: 0; border: none; }
  .register td.status { grid-row: 1; grid-column: 2; justify-self: end; }
  .register td.what, .register td.from, .register td.why { grid-column: 1 / -1; max-width: none; }
  .register td.what { font-weight: 500; margin-top: .15rem; }
  .register tr.open { background: rgba(176, 138, 62, .12); box-shadow: inset 3px 0 0 var(--brass); padding-left: .7rem; }
  .register tr.open td, .register tr.open td:first-child { background: none; box-shadow: none; padding-left: 0; }
}

/* status stamps */
.stamp { display: inline-block; font: 600 .62rem/1 var(--sans); letter-spacing: .12em; text-transform: uppercase;
         padding: .3rem .45rem .26rem; border: 1.5px solid currentColor; border-radius: 2px; white-space: nowrap; }
.stamp.green { color: var(--ok); } .stamp.blue { color: var(--blue); } .stamp.gray { color: var(--muted); }
.stamp.orange { color: var(--orange); } .stamp.violet { color: var(--violet); } .stamp.red { color: var(--red); }
.stamp.red, .stamp.orange { background: color-mix(in srgb, currentColor 9%, transparent); }

/* case file header */
.casefile { border-top: 3px double var(--ink); padding-top: 1.1rem; margin-top: .9rem; }
.casefile .top { display: flex; align-items: center; gap: .8rem; flex-wrap: wrap; }
.casefile .ref { font: 500 .92rem var(--mono); color: var(--green); }
.casefile .title { font: 500 2rem/1.15 var(--serif); color: var(--ink); margin: .45rem 0 .35rem; }
.casefile .meta { font: 400 .86rem/1.5 var(--sans); color: var(--muted); }
.casefile .meta b { font-weight: 600; color: var(--ink); }

/* forms, notes, tabs, expanders */
[data-testid="stForm"] { background: var(--card); }
.form-title { font: 500 1.15rem/1.2 var(--serif); color: var(--ink); }
.form-note { font: 400 .8rem/1.4 var(--sans); color: var(--muted); margin-top: .2rem; }
[data-testid="stAlertContainer"] { border-left: 3px solid currentColor; border-radius: 2px; }
[data-testid="stExpander"] details { background: var(--card); }

/* sidebar */
.wordmark { padding: .1rem 0 1.1rem; margin-bottom: .4rem; border-bottom: 1px solid #335A4A; }
.wordmark .mark { font: 500 2.7rem/1 var(--serif); color: #E3C77F; letter-spacing: .03em; }
.wordmark .sub { font: 600 .64rem/1 var(--sans); letter-spacing: .22em; text-transform: uppercase; color: #CFC6AF;
                 margin-top: .5rem; }
.wordmark .note { font: italic 400 .9rem/1.4 var(--serif); color: #A8B8AB; margin-top: .75rem; }
[data-testid="stSidebar"] .section-label { color: #A8B8AB; margin-top: 1.1rem; }
[data-testid="stSidebar"] .section-label::after { border-top-color: #335A4A; }
/* the sidebar's popover opens with the sidebar's dark green but the main text colour, so lighten its text */
[data-testid="stPopoverBody"] [data-testid="stCaptionContainer"],
[data-testid="stPopoverBody"] [data-testid="stCaptionContainer"] p { color: #D9D1BC; }
</style>"""
st.html(STYLE)


# ---- background inbox watcher (one per server process) -----------------------------------

@st.cache_resource
def start_watcher() -> threading.Event:
    stop = threading.Event()

    def loop():
        while not stop.wait(config.watch_interval()):
            try:
                orchestrator.process_all()
            except Exception:
                logging.getLogger("act_flow").exception("Background processing failed")

    threading.Thread(target=loop, name="inbox-watcher", daemon=True).start()
    return stop


def restart_demo() -> None:
    """Wipe everything and load the six sample emails again, as `python -m act_flow demo` does."""
    with store.LOCK:            # keeps the background watcher out while the data is replaced
        demo.reset(keep_logs=True)
        demo.run()


@st.cache_resource(show_spinner="Setting up the demo…")
def first_run() -> bool:
    """On a fresh copy (for example a new cloud container) there is no data, so set the demo up once."""
    if any(config.sub("samples").glob("*.eml")):
        return False
    with store.session() as con:
        if store.list_cases(con):
            return False
    restart_demo()
    return True


first_run()
if os.environ.get("ACT_NO_WATCH") != "1":
    start_watcher()


# ---- helpers ---------------------------------------------------------------------------------

e = html.escape


def flash(kind: str, message: str) -> None:
    st.session_state["flash"] = (kind, message)


def act(fn, *args) -> None:
    try:
        flash("success", fn(*args))
    except review.ReviewError as err:
        flash("error", str(err))
    st.rerun()


def stamp(status: str) -> str:
    label, colour = STATUS.get(status, (status, "gray"))
    return f'<span class="stamp {colour}">{e(label)}</span>'


def masthead(kicker: str, title: str) -> None:
    d = config.today()
    st.html(f'<div class="mast"><div><div class="kicker">{e(kicker)}</div><div class="title">{e(title)}</div></div>'
            f'<div class="dateline">{d:%A} {d.day} {d:%B %Y}</div></div>')


def section_label(text: str) -> None:
    st.html(f'<div class="section-label">{e(text)}</div>')


def form_title(title: str, note: str | None = None) -> None:
    st.html(f'<div class="form-title">{e(title)}</div>' + (f'<div class="form-note">{e(note)}</div>' if note else ""))


def opened(value: str) -> str:
    moment = dt.datetime.fromisoformat(value)
    return f"{moment.day} {moment:%b %Y, %H:%M} UTC"


def highlighted(text: str, quotes: list[str]) -> str:
    out = html.escape(text)
    for q in sorted({q for q in quotes if q and len(q) > 2}, key=len, reverse=True):
        out = re.sub(re.escape(html.escape(q)), lambda m: f"<mark>{m.group(0)}</mark>", out)
    return f'<div style="white-space:pre-wrap;font-size:0.9rem;line-height:1.55">{out}</div>'


def flat(record: dict) -> dict:
    return {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in record.items()}


def load():
    with store.session() as con:
        return store.list_cases(con)


# ---- sidebar -------------------------------------------------------------------------------------

with st.sidebar:
    st.html('<div class="wordmark"><div class="mark">ACT</div><div class="sub">Instruction desk</div>'
            '<div class="note">A working demonstration. Every client, person and email in it is made up.</div></div>')
    user = st.selectbox("Acting as", list(config.USERS), key="user",
                        format_func=lambda u: f"{config.USERS[u]['name']} – {config.USERS[u]['title']}")
    view = st.radio("View", ["Cases", "Systems", "Audit log"], key="view")

    section_label("Inbox")
    sample_names = [p.stem for p in sorted(config.sub("samples").glob("*.eml"))]
    if sample_names:
        chosen = st.selectbox("Sample email", sample_names, key="sample")
        if st.button("Send it", key="send-sample", icon=":material/send:"):
            samples.send(chosen)
            orchestrator.process_all()
            flash("success", f"Sent {chosen} and processed the inbox.")
            st.rerun()
    if st.button("Process inbox now", key="process", icon=":material/refresh:"):
        orchestrator.process_all()
        st.rerun()
    inbox = config.sub("inbox")
    try:
        inbox_shown = inbox.relative_to(config.PROJECT_DIR).as_posix()
    except ValueError:
        inbox_shown = str(inbox)
    st.caption(f"The inbox (`{inbox_shown}`) is also checked every {config.watch_interval():.0f} seconds.")

    section_label("Demo")
    with st.popover("Start the demo again", icon=":material/restart_alt:", width="stretch"):
        st.caption("Deletes every case and change, then sends the six sample emails in again. "
                   "Anyone else using this copy sees the reset too.")
        if st.button("Yes, start again", key="restart-demo", type="primary"):
            with st.spinner("Setting up the demo…"):
                try:
                    restart_demo()
                    flash("success", "The demo has started again with the six sample emails.")
                except OSError as err:
                    flash("error", f"Could not reset the demo data: {err}")
            st.session_state.pop("ref", None)
            st.rerun()

if "flash" in st.session_state:
    kind, message = st.session_state.pop("flash")
    (st.success if kind == "success" else st.error)(message)


# ---- case detail ------------------------------------------------------------------------------------

def actions(case: dict) -> None:
    ref, status, itype, data = case["ref"], case["status"], case.get("instruction_type"), case["data"]
    forms = []
    if status == "awaiting_review":
        forms.append("approve")
    if status in ("awaiting_review", "awaiting_client"):
        forms.append("correct")
    if status == "awaiting_review" and itype in SCHEMAS:
        forms.append("ask")
    if status == "held_security":
        forms.append("release")
    if status == "exception":
        forms.append("retry")
    if status in ("exception", "awaiting_signature"):
        forms.append("cancel")
    if status == "awaiting_signature":
        forms.append("signature")
    if status in ("awaiting_review", "held_security", "awaiting_client", "exception"):
        forms.append("reject")
    if not forms:
        return
    section_label("Decision")
    columns = st.columns(min(len(forms), 3))
    for i, name in enumerate(forms):
        with columns[i % len(columns)]:
            with st.form(f"{name}-{ref}"):
                if name == "approve":
                    rv = data.get("review", {})
                    needs = ", ".join(config.ROLE_LABELS[r] for r in rv.get("roles_needed", [])) or "any reviewer"
                    form_title("Approve", f"Needs {needs}")
                    for a in rv.get("approvals", []):
                        st.markdown(f":material/check_circle: {config.USERS[a['user']]['name']} ({a['role']})"
                                    + (" · call-back confirmed" if a.get("callback_confirmed") else ""))
                    note = st.text_input("Note", key=f"approve-note-{ref}")
                    callback = False
                    if itype == "bank_details_change":
                        callback = st.checkbox("I called the client on the number already on file and they "
                                               "confirmed these details", key=f"callback-{ref}")
                    if st.form_submit_button("Approve", type="primary", key=f"approve-btn-{ref}"):
                        act(review.approve, ref, st.session_state.user, note, callback)
                elif name == "correct":
                    form_title("Correct a value", "The case is checked again after the change.")
                    names = ["instruction_type"] + ([f.name for f in SCHEMAS[itype].fields] if itype in SCHEMAS else [])
                    field = st.selectbox("Field", names, key=f"correct-field-{ref}")
                    value = st.text_input("New value", key=f"correct-value-{ref}",
                                          help="For instruction_type use one of: " + ", ".join(SCHEMAS))
                    reason = st.text_input("Reason (kept in the audit log)", key=f"correct-reason-{ref}")
                    if st.form_submit_button("Save and re-check", key=f"correct-btn-{ref}"):
                        act(review.correct, ref, st.session_state.user, field, value, reason)
                elif name == "ask":
                    form_title("Ask the client", "Sends one email asking only for what is missing.")
                    fields = st.multiselect("What do you need?", [f.name for f in SCHEMAS[itype].fields],
                                            format_func=lambda n: SCHEMAS[itype].get(n).description,
                                            key=f"ask-fields-{ref}")
                    note = st.text_input("Note to include", key=f"ask-note-{ref}")
                    if st.form_submit_button("Send request", key=f"ask-btn-{ref}"):
                        act(review.ask_client, ref, st.session_state.user, fields, note)
                elif name == "release":
                    form_title("Release after a security check")
                    note = st.text_input("How did you check the sender?", key=f"release-note-{ref}")
                    if st.form_submit_button("Release", key=f"release-btn-{ref}"):
                        act(review.release, ref, st.session_state.user, note)
                elif name == "retry":
                    form_title("Retry", "Carries on from the step that failed. Steps already done are not repeated.")
                    if st.form_submit_button("Retry now", type="primary", key=f"retry-btn-{ref}"):
                        act(review.retry, ref, st.session_state.user)
                elif name == "cancel":
                    form_title("Cancel and undo", "Puts every record this case changed back as it was.")
                    reason = st.text_input("Reason", key=f"cancel-reason-{ref}")
                    if st.form_submit_button("Cancel case", key=f"cancel-btn-{ref}"):
                        act(review.cancel, ref, st.session_state.user, reason)
                elif name == "signature":
                    form_title("E-signature", "Stands in for the signing service telling us the letter is signed.")
                    if st.form_submit_button("Mark as signed", key=f"sign-btn-{ref}"):
                        act(review.signature_received, ref)
                elif name == "reject":
                    form_title("Reject")
                    reason = st.text_input("Reason", key=f"reject-reason-{ref}")
                    if st.form_submit_button("Reject", key=f"reject-btn-{ref}"):
                        act(review.reject, ref, st.session_state.user, reason)


def show_case(ref: str) -> None:
    with store.session() as con:
        case = store.get_case(con, ref)
        msgs_in = store.case_messages(con, ref, "in")
        msgs_out = store.case_messages(con, ref, "out")
        docs = store.case_documents(con, ref)
        steps = store.case_steps(con, ref)
        writes = store.case_writes(con, ref)
        events = store.timeline(con, ref)
    data, itype = case["data"], case.get("instruction_type")
    val = data.get("validation", {})
    fields = val.get("fields", {})

    st.html(f'<div class="casefile"><div class="top"><span class="ref">{e(ref)}</span>{stamp(case["status"])}</div>'
            f'<div class="title">{e(type_label(itype))}</div>'
            f'<div class="meta">From <b>{e(case.get("sender") or "unknown sender")}</b> · '
            f'client <b>{e(case.get("client_id") or "not identified")}</b> · opened {opened(case["created_at"])}</div>'
            f'</div>')
    reasons = data.get("review", {}).get("reasons") or data.get("route", {}).get("reasons") or []
    if data.get("exception"):
        st.error(data["exception"]["message"], icon=":material/error:")
    elif case["status"] == "awaiting_client":
        st.info(case.get("reason") or "Waiting for the client.", icon=":material/hourglass_top:")
    elif reasons and case["status"] not in ("completed", "cancelled", "rejected"):
        st.warning("**Why it is here**\n\n" + "\n".join(f"- {r}" for r in reasons), icon=":material/info:")

    actions(case)

    section_label("The file")
    tabs = st.tabs(["Extracted values", "Evidence", "System updates", "Emails and documents", "Audit trail"])
    with tabs[0]:
        if not fields:
            st.info("Nothing has been extracted yet." if case["status"] != "held_security"
                    else "Held before the AI read it, so nothing has been extracted.")
        else:
            ex = data.get("extraction", {})
            st.caption(f"Read by **{ex.get('extractor')}** {ex.get('model') or ''} · prompt {ex.get('prompt_version')} "
                       f"· schema {ex.get('schema_version')} · overall confidence {val.get('confidence', 0):.0%}")
            rows = [{"Field": n, "Value": f.get("value") or "",
                     "Confidence": f.get("confidence") or 0.0, "Source": f.get("source") or "",
                     "Supporting text": f.get("quote") or "",
                     "Check": "; ".join(f.get("messages", [])) or ("OK" if f["status"] == "ok" else f["status"]),
                     "Corrected by": config.USERS[f["corrected_by"]]["name"] if f.get("corrected_by") else ""}
                    for n, f in fields.items()]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
                "Confidence": st.column_config.ProgressColumn("Confidence", min_value=0.0, max_value=1.0, format="percent")})
            if ex.get("other_requests"):
                st.info("**Other requests noted:** " + "; ".join(ex["other_requests"]))
            section_label("Checks")
            st.dataframe(pd.DataFrame([{"Check": c["check"], "Result": "Pass" if c["result"] == "pass" else "Fail",
                                        "Detail": c["detail"]} for c in val.get("checks", [])]),
                         hide_index=True, width="stretch")
    with tabs[1]:
        quotes = [f.get("quote") for f in fields.values()]
        for i, m in enumerate(msgs_in):
            h, auth = m["headers"], m["headers"].get("auth", {})
            with st.expander(f"{'Original email' if i == 0 else f'Reply {i}'} from {h['from']} · SPF {auth.get('spf')} "
                             f"· DKIM {auth.get('dkim')} · DMARC {auth.get('dmarc')}", expanded=i == 0):
                st.markdown(highlighted(h["body"], quotes), unsafe_allow_html=True)
                st.caption(f"Stored unaltered · SHA-256 {m['sha256'][:16]}…")
                st.download_button("Download original .eml", Path(m["path"]).read_bytes(), Path(m["path"]).name,
                                   key=f"eml-{m['message_id']}")
        for n, d in enumerate(docs, 1):
            with st.expander(f"doc{n}: {d['filename']} · {d['doc_type'] or 'not read yet'}"):
                text = "\n\n".join(f"[page {p}]\n{t}" for p, t in enumerate(d["pages"] or [], 1)) or "(not readable)"
                st.markdown(highlighted(text, quotes), unsafe_allow_html=True)
                st.download_button("Download original", Path(d["path"]).read_bytes(), d["filename"], key=f"doc-{d['id']}")
    with tabs[2]:
        if not steps:
            st.info("No system has been changed yet.")
        else:
            st.dataframe(pd.DataFrame([{
                "#": s["seq"], "Step": s["op"], "Status": s["status"], "Attempts": s["attempts"],
                "Result": json.loads(s["result"]).get("summary", "") if s["result"] else (s["error"] or "")}
                for s in steps]), hide_index=True, width="stretch")
            section_label("Records changed, with before and after kept for audit and undo")
            for w in writes:
                label = "new record" if w["before"] is None else "changed"
                with st.expander(f"{systems.LABELS[w['system']]} · {w['collection']}/{w['record_id']} · {label}"
                                 + (" · undone" if w["undone"] else "")):
                    left, right = st.columns(2)
                    left.caption("Before")
                    left.json(w["before"] or {})
                    right.caption("After")
                    right.json(w["after"])
    with tabs[3]:
        if data.get("tasks"):
            section_label("Tasks created")
            st.markdown("\n".join(f"- {t}" for t in data["tasks"]))
        for m in msgs_out:
            h = m["headers"]
            with st.expander(f"Email sent: {h.get('subject')}"):
                st.text(h.get("body", ""))
        filed = systems.system("dms").find("files", case_ref=ref) if case.get("client_id") else []
        for f in filed:
            path = config.data_dir() / f["path"]
            if path.exists():
                st.download_button(f"{f['folder']}/{f['filename']}" + (f" ({f['signature_status']})" if f.get("signature_status") else ""),
                                   path.read_bytes(), f["filename"], key=f"dms-{f['file_id']}", icon=":material/description:")
        if not (msgs_out or filed or data.get("tasks")):
            st.info("Nothing sent or generated yet.")
    with tabs[4]:
        st.dataframe(pd.DataFrame([{"Time (UTC)": ev["at"][11:19], "Actor": ev["actor"], "Event": ev["event"],
                                    "Detail": json.dumps(ev["detail"])} for ev in events]),
                     hide_index=True, width="stretch")


# ---- views ----------------------------------------------------------------------------------------

def summary_strip(cases: list[dict]) -> None:
    done = [c for c in cases if c["status"] == "completed"]
    straight = sum(1 for c in done if c["data"].get("straight_through"))
    held = sum(c["status"] in ("held_security", "exception") for c in cases)
    cells = [("Instructions", len(cases), ""), ("Completed", len(done), ""),
             ("Straight through", straight, f"{straight / len(done):.0%}" if done else ""),
             ("Awaiting review", sum(c["status"] == "awaiting_review" for c in cases), ""),
             ("Awaiting client", sum(c["status"] == "awaiting_client" for c in cases), ""),
             ("Held or exception", held, "")]
    st.html('<div class="strip-wrap"><div class="strip">' + "".join(
        f'<div class="{"flag" if label == "Held or exception" and value else ""}"><div class="lbl">{label}</div>'
        f'<div class="num">{value}' + (f"<small>{extra}</small>" if extra else "") + '</div></div>'
        for label, value, extra in cells) + '</div></div>')


def register(cases: list[dict], open_ref: str) -> None:
    rows = "".join(
        f'<tr class="{"open" if c["ref"] == open_ref else ""}"><td class="ref">{e(c["ref"])}</td>'
        f'<td class="what">{e(type_label(c.get("instruction_type")))}</td><td class="status">{stamp(c["status"])}</td>'
        f'<td class="from">{e(c.get("sender") or "")}</td><td class="why">{e(c.get("reason") or "")}</td></tr>'
        for c in cases)
    st.html('<div class="register-wrap"><table class="register"><thead><tr><th>Reference</th><th>Instruction</th>'
            '<th>Status</th><th>From</th><th>Where it stands</th></tr></thead><tbody>' + rows + '</tbody></table></div>')


def cases_view() -> None:
    cases = load()
    masthead("Client instructions · review desk", "The register")
    if not cases:
        st.info("No cases yet. Send a sample email from the sidebar, or use **Start the demo again**.")
        return
    summary_strip(cases)

    refs = [c["ref"] for c in cases]
    default = next((c["ref"] for c in cases if c["status"] in NEEDS_ACTION), refs[0])
    if st.session_state.get("ref") not in refs:
        st.session_state["ref"] = default
    register(cases, st.session_state["ref"])

    labels = {c["ref"]: f"{c['ref']} · {type_label(c.get('instruction_type'))}" for c in cases}
    pick, _ = st.columns([2, 3])
    ref = pick.selectbox("Open case", refs, key="ref", format_func=labels.get)
    show_case(ref)


def systems_view() -> None:
    masthead("Simulated for the demo", "Business systems")
    st.caption("Each system keeps its own records and field names. Simulate a fault to see retries, pausing and undo.")
    for s in systems.all_systems():
        fault = s.fault_status()
        with st.expander(f"{s.label}" + (f" · {fault['remaining']} {fault['kind']} fault(s) queued" if fault["remaining"] else ""),
                         expanded=False):
            for collection, records in s.dump().items():
                section_label(f"{collection} ({len(records)})")
                st.dataframe(pd.DataFrame([flat(r) for r in records]), hide_index=True, width="stretch")
            with st.form(f"fault-{s.name}"):
                kind = st.selectbox("Simulate", ["transient", "permanent"], key=f"fault-kind-{s.name}",
                                    format_func=lambda k: "not responding" if k == "transient" else "rejecting data")
                count = st.number_input("for the next N calls", min_value=0, max_value=50, value=5, key=f"fault-n-{s.name}")
                if st.form_submit_button("Set", key=f"fault-btn-{s.name}"):
                    s.inject_fault(kind, int(count))
                    orchestrator.breaker_reset(s.name)
                    flash("success", f"{s.label}: {int(count)} {kind} fault(s) queued.")
                    st.rerun()


def audit_view() -> None:
    masthead("Append-only · hash-chained", "Audit log")
    with store.session() as con:
        result = store.verify_audit(con)
        rows = [dict(r) for r in con.execute("SELECT * FROM audit ORDER BY seq DESC")]
    if result["ok"]:
        st.success(f"Hash chain intact across {result['events']} events.", icon=":material/verified:")
    else:
        st.error(f"Hash chain broken at event {result['broken_at']}: the log has been altered.")
    st.caption("The database refuses updates and deletes, and each event carries the hash of the one before.")
    st.download_button("Download the full log (JSON lines)", "\n".join(json.dumps(r) for r in reversed(rows)),
                       "act-audit-log.jsonl", key="audit-download", icon=":material/download:")
    st.dataframe(pd.DataFrame([{"#": r["seq"], "Time (UTC)": r["at"][:19].replace("T", " "), "Case": r["case_ref"],
                                "Actor": r["actor"], "Event": r["event"], "Hash": r["hash"][:12]} for r in rows[:300]]),
                 hide_index=True, width="stretch")


{"Cases": cases_view, "Systems": systems_view, "Audit log": audit_view}[view]()
