"""The demonstration scenario, shared by `python -m act_flow demo` and the console's "Start again" button."""
from __future__ import annotations

import os
import shutil
import stat
from typing import Callable

from . import config, orchestrator, review, samples, store, systems
from .extraction import get_extractor


def _force_remove(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def reset(keep_logs: bool = False) -> None:
    """Delete every case, simulated system and email, and the logs unless keep_logs is set.

    The console keeps the logs: on Windows another process may still have the log file open.
    """
    target = config.data_dir()
    if not target.exists():
        return
    if not keep_logs:
        shutil.rmtree(target, onerror=_force_remove)
        return
    for child in target.iterdir():
        if child.name == "logs":
            continue
        if child.is_dir():
            shutil.rmtree(child, onerror=_force_remove)
        else:
            _force_remove(os.unlink, child, None)


def _find(con, itype: str) -> str | None:
    row = con.execute("SELECT ref FROM cases WHERE instruction_type=? ORDER BY ref LIMIT 1", (itype,)).fetchone()
    return row["ref"] if row else None


def run(auto: bool = False, say: Callable[[str], None] = lambda _m: None) -> None:
    """Seed the systems and run every sample email. With auto, also play the reviewers' part.

    Call reset() first for a clean start.
    """
    systems.seed()
    samples.write_samples()
    systems.system("accounting").inject_fault("transient", 1)   # a blip: retried and recovers
    extractor = get_extractor()
    say(f"Extractor: {extractor.name}" + (f" ({extractor.model})" if getattr(extractor, "model", None) else ""))
    names = [p.stem for p in sorted(config.sub("samples").glob("*.eml"))]
    for name in names[:-1]:
        samples.send(name)
    samples.send("01-address-change")                           # the same email again: ignored as a duplicate
    orchestrator.process_all(extractor)
    systems.system("tax").inject_fault("transient", 5)          # an outage: this case pauses and resumes later
    samples.send(names[-1])
    orchestrator.process_all(extractor)

    with store.session() as con:
        starter = _find(con, "payroll_new_starter")
    if starter:
        say(f"\nThe client replies to the request for information on {starter}...")
        samples.send_reply(starter, "Hi,\n\nSam's date of birth is 14 March 1998.\n\nThanks,\nHannah",
                           "<sample-02b-reply@harbourlane.example>")
        orchestrator.process_all(extractor)

    if not auto:
        return
    with store.session() as con:
        onboarding, bank, records = (_find(con, t) for t in
                                     ("new_client_onboarding", "bank_details_change", "records_submission"))
    say("\nPlaying the reviewers' part:")
    steps = [
        (starter, lambda r: review.approve(r, "alex.morgan", "Checked against the starter checklist")),
        (onboarding, lambda r: review.approve(r, "jordan.lee", "ID and address checked")),
        (bank, lambda r: review.approve(r, "casey.brown", "Called Hannah on the number on file", True)),
        (bank, lambda r: review.approve(r, "robin.shah", "Second approval", True)),
    ]
    for ref, action in steps:
        if ref:
            say(f"  {ref}: {action(ref)}")
    systems.system("tax").clear_faults()
    if records:
        say(f"  {records}: tax system back; {review.retry(records, 'casey.brown')}")
    if onboarding:
        say(f"  {onboarding}: {review.signature_received(onboarding)}")
