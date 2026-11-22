"""Command line: python -m act_flow <command>. Run with no command for help."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from . import config, demo, orchestrator, samples, store, systems
from .extraction import type_label


def _setup_logging() -> None:
    log_file = config.sub("logs") / "act-flow.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()])


def cmd_reset(_args) -> None:
    demo.reset()
    print(f"Cleared {config.data_dir()}")


def cmd_seed(_args) -> None:
    systems.seed()
    print("Seeded the simulated CRM, accounting, tax, payroll and document systems with synthetic clients.")


def cmd_samples(_args) -> None:
    names = samples.write_samples()
    print("Sample emails written to", config.sub("samples"))
    for n in names:
        print("  ", n)


def cmd_send(args) -> None:
    names = [p.stem for p in sorted(config.sub("samples").glob("*.eml"))] if args.name == "all" else [args.name]
    for n in names:
        print("Sent", samples.send(n))


def print_status() -> None:
    with store.session() as con:
        cases = list(reversed(store.list_cases(con)))
    if not cases:
        print("No cases yet.")
        return
    print(f"{'Case':<16} {'Status':<19} {'Instruction':<38} Where it stands")
    for c in cases:
        print(f"{c['ref']:<16} {c['status']:<19} {type_label(c.get('instruction_type')):<38} {(c.get('reason') or '')[:90]}")


def cmd_process(_args) -> None:
    orchestrator.process_all()
    print_status()


def cmd_status(_args) -> None:
    print_status()


def cmd_watch(_args) -> None:
    print(f"Watching {config.sub('inbox')} every {config.watch_interval():.0f}s. Press Ctrl+C to stop.")
    while True:
        orchestrator.process_all()
        time.sleep(config.watch_interval())


def cmd_serve(args) -> None:
    import subprocess
    env = os.environ.copy()
    if args.no_watch:
        env["ACT_NO_WATCH"] = "1"
    print(f"Review console: http://localhost:{args.port}  (Ctrl+C to stop)")
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(config.PROJECT_DIR / "console.py"),
                    "--server.port", str(args.port), "--server.address", "localhost"],
                   env=env, cwd=config.PROJECT_DIR)


def cmd_verify(_args) -> None:
    with store.session() as con:
        result = store.verify_audit(con)
    print("Audit chain intact" if result["ok"] else f"Audit chain BROKEN at event {result['broken_at']}",
          f"({result['events']} events)")
    sys.exit(0 if result["ok"] else 1)


def cmd_fault(args) -> None:
    systems.system(args.system).inject_fault(args.kind, args.count)
    print(f"{systems.LABELS[args.system]} will fail the next {args.count} calls ({args.kind}).")


def cmd_demo(args) -> None:
    """Reset, seed and run every sample. With --auto, also play the people's part."""
    cmd_reset(args)
    _setup_logging()
    demo.run(auto=args.auto, say=print)
    print()
    print_status()
    if not args.auto:
        print("\nNext: run `python -m act_flow serve` and open http://localhost:8501 to review the waiting cases.")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="act_flow", description="ACT instruction automation (demo)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("reset", help="delete all demo data").set_defaults(fn=cmd_reset)
    sub.add_parser("seed", help="load synthetic clients into the simulated systems").set_defaults(fn=cmd_seed)
    sub.add_parser("samples", help="write the sample emails").set_defaults(fn=cmd_samples)
    p = sub.add_parser("send", help="drop a sample email into the inbox")
    p.add_argument("name", help="sample name (for example 01-address-change) or 'all'")
    p.set_defaults(fn=cmd_send)
    sub.add_parser("process", help="process the inbox and every case once").set_defaults(fn=cmd_process)
    sub.add_parser("status", help="list cases").set_defaults(fn=cmd_status)
    sub.add_parser("watch", help="process the inbox continuously").set_defaults(fn=cmd_watch)
    p = sub.add_parser("serve", help="start the review console (and the inbox watcher)")
    p.add_argument("--port", type=int, default=8501)
    p.add_argument("--no-watch", action="store_true")
    p.set_defaults(fn=cmd_serve)
    sub.add_parser("verify-audit", help="check the audit log's hash chain").set_defaults(fn=cmd_verify)
    p = sub.add_parser("fault", help="make a simulated system fail its next calls")
    p.add_argument("system", choices=list(systems.LABELS))
    p.add_argument("kind", choices=["transient", "permanent"])
    p.add_argument("count", type=int)
    p.set_defaults(fn=cmd_fault)
    p = sub.add_parser("demo", help="reset, seed and run all the samples")
    p.add_argument("--auto", action="store_true", help="also approve, retry and sign as the reviewers would")
    p.set_defaults(fn=cmd_demo)
    args = parser.parse_args(argv)
    if args.command not in ("reset", "demo", "serve"):   # serve only starts Streamlit; don't hold the log open
        _setup_logging()
    args.fn(args)


if __name__ == "__main__":
    main()
