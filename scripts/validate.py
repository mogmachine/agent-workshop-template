#!/usr/bin/env python3
"""
validate.py - Task completion validation (Ralph-loop style).

Run AFTER claiming a task is "done" to self-verify the outcome.

Usage:
    python3 scripts/validate.py file_written <path> [--contains "substring"]
    python3 scripts/validate.py file_exists <path>
    python3 scripts/validate.py url_reachable <url> [--status 200]
    python3 scripts/validate.py git_committed "<message substring>"
    python3 scripts/validate.py command_ok "<cmd>"
    python3 scripts/validate.py report
    python3 scripts/validate.py gate --task "..." --evidence "checks|...|..."

`report` mode: prints a template for an agent to fill in verifying what it did.
`gate` mode: runs a list of validation checks; exit 0 only if ALL pass. Use as the last step before claiming done.

Exit code 0 = validated, proceed.
Exit code 1 = validation FAILED, the claim is a phantom completion — retry or escalate.
"""
import sys
import os
import subprocess
import urllib.request
import urllib.error
from pathlib import Path


def fail(msg: str):
    print(f"❌ VALIDATION FAILED: {msg}", file=sys.stderr)
    sys.exit(1)


def ok(msg: str):
    print(f"✅ {msg}", file=sys.stderr)
    sys.exit(0)


def check_file_written(path: str, contains: str | None = None):
    p = Path(path)
    if not p.exists():
        fail(f"File does not exist: {path}")
    if p.stat().st_size == 0:
        fail(f"File is empty: {path}")
    if contains:
        try:
            content = p.read_text(errors="replace")
        except Exception as e:
            fail(f"Could not read {path}: {e}")
        if contains not in content:
            fail(f"File {path} does not contain expected substring: {contains!r}")
    ok(f"File written: {path} ({p.stat().st_size} bytes)" +
       (f", contains {contains!r}" if contains else ""))


def check_file_exists(path: str):
    p = Path(path)
    if not p.exists():
        fail(f"File does not exist: {path}")
    ok(f"File exists: {path}")


def check_url(url: str, expected_status: int = 200):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "agent-workshop-validate/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != expected_status:
                fail(f"URL {url} returned {resp.status}, expected {expected_status}")
        ok(f"URL reachable: {url} (status {expected_status})")
    except urllib.error.HTTPError as e:
        if e.code == expected_status:
            ok(f"URL reachable: {url} (status {e.code})")
        fail(f"URL {url} HTTP error {e.code}")
    except Exception as e:
        fail(f"URL {url} unreachable: {e}")


def check_git_committed(msg_substr: str):
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=str(Path(__file__).resolve().parent.parent),
            text=True,
        ).strip()
    except subprocess.CalledProcessError as e:
        fail(f"git log failed: {e}")
    if msg_substr.lower() not in out.lower():
        fail(f"Last commit message does not contain {msg_substr!r}: got {out!r}")
    ok(f"Git commit verified: {out}")


def check_command_ok(cmd: str):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            fail(f"Command failed (exit {result.returncode}): {cmd}\nstderr: {result.stderr[:500]}")
        ok(f"Command ok: {cmd}")
    except subprocess.TimeoutExpired:
        fail(f"Command timed out: {cmd}")


def print_report_template():
    print("""
=== TASK COMPLETION REPORT ===
Fill in each section. If you cannot fill it in, the task is not done.

1. What was the task?
   >

2. What did I actually do? (commands/tools used)
   >

3. What evidence proves it was done?
   - File written: <path> (run: validate.py file_written <path>)
   - URL live: <url>    (run: validate.py url_reachable <url>)
   - Git commit: <hash> (run: validate.py git_committed "<msg>")
   - Message sent: <channel/id>

4. What could still be wrong? (honest self-audit)
   >

5. Next action / handoff:
   >
""", file=sys.stderr)
    sys.exit(0)


def check_gate(task: str, checks: list[str]):
    """Run a pipe-separated list of subcommands. ALL must pass.

    Each check is a validate.py subcommand string, e.g.
        file_written drafts/x.md --contains hello
        url_reachable https://example.com
        command_ok systemctl is-active nginx
    """
    import shlex
    script = Path(__file__).resolve()
    failed = []
    for c in checks:
        c = c.strip()
        if not c:
            continue
        argv = [sys.executable, str(script)] + shlex.split(c)
        result = subprocess.run(argv, capture_output=True, text=True)
        status = "PASS" if result.returncode == 0 else "FAIL"
        out = (result.stderr or result.stdout).strip().split("\n")[-1]
        print(f"  [{status}] {c}  — {out}", file=sys.stderr)
        if result.returncode != 0:
            failed.append(c)
    print("", file=sys.stderr)
    if failed:
        fail(f"Gate for task {task!r} FAILED on {len(failed)} check(s): {failed}")
    ok(f"Gate for task {task!r} PASSED all {len(checks)} check(s)")


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    mode = sys.argv[1]
    args = sys.argv[2:]

    if mode == "file_written":
        if not args:
            fail("Usage: validate.py file_written <path> [--contains X]")
        contains = None
        if "--contains" in args:
            i = args.index("--contains")
            contains = args[i + 1]
            args = args[:i]
        check_file_written(args[0], contains)
    elif mode == "file_exists":
        if not args:
            fail("Usage: validate.py file_exists <path>")
        check_file_exists(args[0])
    elif mode == "url_reachable":
        if not args:
            fail("Usage: validate.py url_reachable <url> [--status N]")
        status = 200
        if "--status" in args:
            i = args.index("--status")
            status = int(args[i + 1])
            args = args[:i]
        check_url(args[0], status)
    elif mode == "git_committed":
        if not args:
            fail("Usage: validate.py git_committed \"<msg substr>\"")
        check_git_committed(args[0])
    elif mode == "command_ok":
        if not args:
            fail("Usage: validate.py command_ok \"<cmd>\"")
        check_command_ok(" ".join(args))
    elif mode == "report":
        print_report_template()
    elif mode == "gate":
        if "--task" not in args or "--evidence" not in args:
            fail("Usage: validate.py gate --task '...' --evidence 'check1|check2|...'")
        task = args[args.index("--task") + 1]
        evidence = args[args.index("--evidence") + 1]
        checks = [c for c in evidence.split("|") if c.strip()]
        if not checks:
            fail("Gate called with no checks. At minimum provide one validation command.")
        check_gate(task, checks)
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
