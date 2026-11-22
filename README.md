# ACT instruction automation – working demo

**Try it online:** https://act-instruction-desk.streamlit.app (works on a phone too). If it has been
asleep, click the button to wake it; it takes about a minute and starts with the six sample emails.

A runnable version of the technical proposal in `../ACT-technical-proposal.docx`. A client emails an
instruction with attachments; the system reads it, checks it, routes it, gets approvals where they
are needed, updates the business systems, creates tasks and letters, replies to the client, and
records every step in a tamper-evident audit log.

It runs on one Windows machine with Python 3.11. ACT's real systems are replaced by simulated ones
(see [What is real and what is simulated](#what-is-real-and-what-is-simulated)). All data is synthetic.

## Quick start

From this folder, in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m act_flow demo
```

```powershell
.\.venv\Scripts\python.exe -m act_flow serve
```

Then open http://localhost:8501. The review console is a Streamlit app. Use **Acting as** in the
sidebar to switch between the four synthetic staff and approve the waiting cases, and **Sample email**
to send more emails in. `demo --auto` plays the reviewers' part as well, so every case finishes.

If the console starts with no data (for example on a fresh copy), it runs the demo by itself.
**Start the demo again** in the sidebar wipes everything and reloads the six sample emails.

If `.venv` is missing (for example on another machine):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## What the demo shows

| Email | What happens |
|---|---|
| Change of address (Harbour Lane Bakery) | Straight through. CRM, accounting, tax and payroll updated; one simulated accounting blip is retried automatically; client gets a confirmation. |
| The same email again | Recognised by its Message-ID and ignored. |
| Payroll new starter, date of birth missing | Client is asked for just the date of birth. Their reply joins the same case, the payroll lead approves, the employee is created, the form is filed and a task is set before the payroll cut-off. |
| New client enquiry (Riverside Joinery) | Companies House and identity-document checks run; compliance must approve. Client, contact, folders and records are created, an engagement letter and welcome letter are generated, and the case waits for the e-signature. |
| Change of bank details | Needs two different people (client manager and finance), and a confirmed call-back to the number on file. Whoever corrects a value cannot approve it. |
| Spoofed bank change (look-alike domain, DMARC fail, prompt injection) | Held for a security check before the AI reads it. No auto-reply. Even if released, an unknown sender's bank change cannot be approved. |
| Records for a tax return, during a tax-system outage | Documents are filed, the tax step fails three times and the case pauses as an exception. **Retry** carries on from the failed step without repeating the filing; **Cancel** undoes every change. |

## Using Claude for the extraction

By default the demo uses a rules-based extractor so it runs offline and costs nothing. To use Claude
instead, set an API key before running (each email then makes one paid API call):

```powershell
$env:ANTHROPIC_API_KEY = "<your key>"
```

The Claude extractor (`act_flow/extraction.py`) uses `claude-opus-5-5` with structured outputs: the
model can only return JSON matching the schema, has no tools and no access to any system, and treats
email content as untrusted data. Every value must come with a source and an exact quote, which the
validation layer checks against the original text, so an invented value is caught and sent to a
person. Refusals and API errors also go to a person, and server-side refusal fallbacks are on. Set
`ACT_EXTRACTOR=rules` to force the offline extractor, or `ACT_CLAUDE_MODEL` to change the model.

## Putting it online (Streamlit Community Cloud)

Vercel can't run this: it only runs short request-and-response functions, while Streamlit needs a
server that stays up and holds a live connection to each visitor. Streamlit Community Cloud is free
and built for it.

1. Push this folder to a GitHub repository (it can be private). `.gitignore` keeps out `.venv`,
   `data` and local settings.
2. At https://share.streamlit.io, sign in with GitHub, choose **Create app**, pick the repository and
   branch, and set the main file to `console.py`. Under **Advanced settings**, choose Python 3.11.
3. Deploy. On first start the console finds no data and sets the demo up by itself.

Things to know:
- Data lives on the app's own disk, so it resets when the app restarts or goes to sleep. Everyone
  with the link shares the same cases; **Start the demo again** puts them back to the start.
- Don't add `ANTHROPIC_API_KEY` to a public app: every email sent in would make a paid API call.
  Without it the app uses the rules-based extractor and costs nothing.

## How the code maps to the proposal

| Proposal section | Code |
|---|---|
| 1 Architecture and integration | `orchestrator.py` (workflow engine), `adapters.py` (one adapter per system, idempotent writes, undo), `systems.py` (simulated systems with their own field names) |
| 2 AI extraction and validation | `extraction.py` (versioned schemas, Claude and rules extractors), `validation.py` (format, grounding, cross-document, Companies House/HMRC, business rules, duplicates, confidence) |
| 3 Workflow automation | `routing.py` (routing table per instruction type), `documents.py` (client emails, PDF letters) |
| 4 Human approval and exceptions | `review.py` (approval rules, four-eyes, call-back, corrections, retry, cancel), `console.py` (Streamlit review console) |
| 5 Security and audit | `intake.py` (immutable evidence, SPF/DKIM/DMARC and look-alike checks, file allow-list), `store.py` (append-only, hash-chained audit log) |
| 6 Testing and monitoring | `tests/` (26 tests), `verify-audit`, `data/logs/act-flow.log`, `demo.py` (the demo scenario) |

## What is real and what is simulated

**Working as described:** the workflow and its states, extraction contract, validation and grounding
checks, routing rules, approval rules, idempotent writes with before/after values, retries with
backoff, circuit breaker, resume and undo, reminders and escalation, evidence storage with hashes,
the hash-chained audit log, generated emails and PDF letters, and the review console.

**Simulated for the demo:**
- The mailbox is a folder (`data/inbox`). Production would use Microsoft Graph change notifications.
- The CRM, accounting, tax, payroll and document systems are local SQLite stand-ins. Production
  adapters would call each vendor's API through API Management.
- Companies House and HMRC VAT lookups read local JSON files.
- E-signature is a button that stands in for the signing service's webhook.
- Document reading uses text extraction from PDFs, CSV and text files; scanned images would need OCR
  (Azure AI Document Intelligence in the proposal).
- Sort codes are checked for format only, not against the published modulus-checking rules.
- Working days ignore bank holidays. Staff sign-in is a drop-down, not Entra ID.

## Commands

| Command | What it does |
|---|---|
| `demo [--auto]` | Reset, seed, and run every sample email |
| `serve [--port 8501]` | Streamlit review console, with a watcher that checks the inbox every 5 seconds |
| `samples` / `send <name or all>` | Write the sample emails / drop one into the inbox |
| `process` / `watch` | Process the inbox once / continuously |
| `status` | List cases |
| `fault <system> transient\|permanent <count>` | Make a simulated system fail its next calls |
| `verify-audit` | Check the audit log's hash chain |
| `seed` / `reset` | Load synthetic clients / delete all demo data |

Run each as `.\.venv\Scripts\python.exe -m act_flow <command>`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The tests cover every route above, plus validation rules, tamper detection in the audit log, the
Claude extractor against a fake client (request shape, refusals, invented values), reminders and
escalation, and the review console (driven headlessly with Streamlit's AppTest).

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `ACT_DATA_DIR` | `./data` | Where cases, evidence, simulated systems and the outbox live |
| `ACT_EXTRACTOR` | `auto` | `auto` (Claude if an API key is set), `claude` or `rules` |
| `ACT_CLAUDE_MODEL` | `claude-opus-5-5` | Model for the Claude extractor |
| `ACT_TODAY` | today | Pretend date, for testing reminders and deadlines |
| `ACT_RETRY_DELAY` | `0.5` | Base delay in seconds between retries |
| `ACT_WATCH_SECONDS` | `5` | How often `serve` and `watch` check the inbox |
| `ACT_ALLOW_TEST_IDS` | `1` | Accept HMRC's reserved `QQ` National Insurance prefix used in the samples |
