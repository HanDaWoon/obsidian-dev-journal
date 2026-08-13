#!/usr/bin/env python3
"""Regression suite for obsidian-dev-journal. No dependencies; run directly:

    python3 tests/test_capture.py

Every test here exists because a real bug got through. Do not delete a case
without understanding which bug it pins down — the comments name them.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAPTURE = ROOT / "scripts" / "capture_session.py"
UTIL = ROOT / "scripts" / "journal_util.py"

FAILURES: list[str] = []
PASSES = 0


def check(cond: bool, label: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  PASS  {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL  {label}")


def load_capture():
    spec = importlib.util.spec_from_file_location("capture_session", CAPTURE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_capture(*args: str, home: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if home:
        env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(CAPTURE), *args], capture_output=True, text=True, env=env
    )


def run_util(*args: str, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(UTIL), *args], capture_output=True, text=True, env=env
    )


def jsonl(path: Path, rows: list[dict], base: dict | None = None) -> Path:
    base = base or {}
    path.write_text(
        "\n".join(json.dumps({**base, **r}, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    return path


def ts(offset_min: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=offset_min)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


# --------------------------------------------------------------------------- #
def test_redaction(m) -> None:
    """Digests land in a synced vault. Every case below leaked at some point."""
    print("\n[redaction]")
    cases = [
        # (input, substring that must NOT survive; None = must be untouched)
        ("export AWS_SECRET_ACCESS_KEY=abc123", "abc123"),
        ('curl -d \'{"api_key": "hunter2"}\'', "hunter2"),          # JSON bodies
        ("aws configure set aws_secret_access_key AKXXXXYYYY", "AKXXXXYYYY"),
        ("mysql --password hunter2 -u root", "hunter2"),            # space-separated flag
        ('curl -H "Authorization: Basic dXNlcjpwYXNzd29yZA=="', "dXNlcjpwYXNz"),
        ("curl -u admin:hunter2 http://prometheus:9090", "hunter2"),  # curl basic auth
        ("wget --user admin:pa55w0rd http://x", "pa55w0rd"),
        ("psql postgres://u:hunter2@host/db", "hunter2"),           # connection string
        ("token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123", "ghp_"),
        ("Bearer sk-abcdefghijklmnop1234", "sk-abc"),
        ("OAUTH_TOKEN=xyz789 ./run.sh", "xyz789"),
        # False positives: these must pass through untouched.
        ("git log --author=handawoon --oneline", None),
        ('git commit --author="Han <h@x.io>"', None),
        ("sort -u file.txt", None),
        ("curl -u admin http://x", None),
        ("just normal text with no secrets", None),
    ]
    for text, leak in cases:
        got = m.redact(text)
        if leak is None:
            check(got == text, f"untouched: {text[:44]}")
        else:
            check(leak not in got, f"redacted:  {text[:44]}")

    # Linear-time guarantee. A leading `.*` in the secret regex once made this
    # 58s on a 12MB transcript; keep the two-stage design.
    start = time.time()
    for _ in range(2000):
        m.redact("y" * 600)
    check(time.time() - start < 2.0, "redaction stays linear (2000x600 chars < 2s)")


def test_hostile_input(tmp: Path) -> None:
    """Format drift must cost one entry, never the whole digest."""
    print("\n[hostile transcript shapes]")
    rows = [
        {"type": "user", "message": "plain string message", "cwd": "/repo/x"},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": None}]}},
        {"type": "assistant", "message": {"role": "assistant", "model": {"d": 1}, "content": []}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": None, "name": None, "input": "notadict"}],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": 123, "content": {"a": {"b": 1}}}],
            },
        },
        {"timestamp": 12345, "type": "user", "message": {"role": "user", "content": "정상 1"}},
        {"type": "user", "message": {"role": "user", "content": "정상 2"}},
    ]
    f = jsonl(tmp / "hostile.jsonl", rows)
    r = run_capture("--transcript", str(f), "--cwd", "/repo/x", "--session-id", "h1", "--stdout")
    check(r.returncode == 0, "hostile input exits 0")
    check("정상 1" in r.stdout and "정상 2" in r.stdout, "good entries survive bad neighbours")

    # Empty / garbage / missing must all be non-fatal.
    (tmp / "empty.jsonl").write_text("", encoding="utf-8")
    check(run_capture("--transcript", str(tmp / "empty.jsonl")).returncode == 0, "empty transcript")
    (tmp / "garbage.jsonl").write_text("not json\n{\n", encoding="utf-8")
    check(
        run_capture("--transcript", str(tmp / "garbage.jsonl"), "--force").returncode == 0,
        "garbage transcript",
    )
    check(run_capture("--transcript", str(tmp / "nope.jsonl")).returncode == 0, "missing transcript")


def test_extraction(tmp: Path) -> None:
    print("\n[extraction]")
    base = dict(cwd="/repo/api", sessionId="ex1", gitBranch="feat/x", isSidechain=False)
    rows = [
        {"type": "user", "message": {"role": "user", "content": "리미터 붙여줘"}, "timestamp": ts(20)},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {"question": "저장소?", "options": [{"label": "Redis"}, {"label": "dict"}]}
                            ]
                        },
                    }
                ],
            },
            "timestamp": ts(19),
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": 'The user answered: "저장소?"="Redis"'}
                ],
            },
            "timestamp": ts(18),
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "<synthetic>",
                "content": [
                    {"type": "tool_use", "id": "t2", "name": "ExitPlanMode", "input": {"plan": "1. a\n2. b"}}
                ],
            },
            "timestamp": ts(17),
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "pytest -x"}}],
            },
            "timestamp": ts(16),
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t3", "content": "KeyError: 'window'", "is_error": True}
                ],
            },
            "timestamp": ts(15),
        },
        # Noise that must be filtered out of "prompts".
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta noise"}, "timestamp": ts(14)},
        {"type": "user", "message": {"role": "user", "content": "<command-name>/clear</command-name>"}, "timestamp": ts(13)},
        {"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": [{"type": "text", "text": "sidechain"}]}, "timestamp": ts(12)},
        {"type": "summary", "summary": "s", "leafUuid": "x"},
    ]
    f = jsonl(tmp / "extract.jsonl", rows, base)
    # --force: this fixture has one real prompt on purpose (the rest is noise we
    # want filtered), which would otherwise trip the min-signal threshold and
    # produce no digest at all — making every assertion below vacuous.
    out = run_capture(
        "--transcript", str(f), "--cwd", "/repo/api", "--session-id", "ex1", "--force", "--stdout"
    ).stdout
    check(out.strip() != "", "digest produced (guards against vacuous assertions)")
    check("**chose:** Redis" in out, "AskUserQuestion answer parsed to bare label")
    check("1. a\n2. b" in out, "ExitPlanMode plan keeps newlines")
    check("KeyError: 'window'" in out, "error captured")
    check("pytest -x" in out, "interesting command captured")
    check("meta noise" not in out and "/clear" not in out, "meta/command noise filtered")
    check("sidechain" not in out, "sidechain entries excluded")
    check("<synthetic>" not in out, "synthetic model label excluded")


def test_thresholds(tmp: Path) -> None:
    print("\n[signal thresholds]")
    base = dict(cwd="/repo/m", sessionId="mf1", isSidechain=False)
    # 1 prompt + 1 file edit must pass (min_files_touched off-by-one).
    rows = [
        {"type": "user", "message": {"role": "user", "content": "파일 하나만"}, "timestamp": ts(5)},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "m",
                "content": [{"type": "tool_use", "id": "t", "name": "Edit", "input": {"file_path": "/repo/m/x.py"}}],
            },
            "timestamp": ts(4),
        },
    ]
    f = jsonl(tmp / "minf.jsonl", rows, base)
    out = run_capture("--transcript", str(f), "--cwd", "/repo/m", "--session-id", "mf1", "--stdout").stdout
    check("files_touched: 1" in out, "single file edit counts as signal")

    # 1 prompt + 0 edits must be skipped.
    f2 = jsonl(tmp / "thin.jsonl", [rows[0]], base)
    r = run_capture("--transcript", str(f2), "--cwd", "/repo/m", "--session-id", "th1")
    check("below signal threshold" in r.stdout, "one-prompt session skipped")


def test_worktree_and_git(tmp: Path) -> None:
    print("\n[worktree + git]")
    if not shutil.which("git"):
        print("  SKIP  git not available")
        return
    repo = tmp / "wt" / "my-api"
    repo.mkdir(parents=True)

    def g(cwd: Path, *a: str, **kw) -> None:
        subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, **kw)

    g(repo, "init", "-q")
    g(repo, "config", "user.email", "t@t.io")
    g(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("hi")
    g(repo, "add", "-A")
    env = dict(os.environ, GIT_COMMITTER_DATE="2026-03-10T03:00:00+00:00")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "inside window", "--date=2026-03-10T03:00:00+00:00"],
        capture_output=True, env=env,
    )
    (repo / "b.txt").write_text("b")
    g(repo, "add", "-A")
    env2 = dict(os.environ, GIT_COMMITTER_DATE="2026-08-01T12:00:00+00:00")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "outside window", "--date=2026-08-01T12:00:00+00:00"],
        capture_output=True, env=env2,
    )
    g(repo, "worktree", "add", "-q", str(tmp / "wt" / "feat-x"), "-b", "feat-x")

    base = dict(cwd=str(tmp / "wt" / "feat-x"), sessionId="w1", isSidechain=False)
    rows = [
        {"type": "user", "message": {"role": "user", "content": "worktree 작업"}, "timestamp": ts(10)},
        {"type": "user", "message": {"role": "user", "content": "p2"}, "timestamp": ts(9)},
    ]
    f = jsonl(tmp / "wt.jsonl", rows, base)
    out = run_capture("--transcript", str(f), "--cwd", str(tmp / "wt" / "feat-x"), "--session-id", "w1", "--stdout").stdout
    check('repo: "my-api"' in out, "worktree attributed to canonical repo (--git-common-dir)")
    check('worktree: "feat-x"' in out, "worktree name recorded separately")

    # cwd resolved from the transcript, not the payload — keeps PreCompact and
    # SessionEnd captures of one session on the same filename.
    out2 = run_capture("--transcript", str(f), "--cwd", "/somewhere/else", "--session-id", "w1", "--stdout").stdout
    check('repo: "my-api"' in out2, "transcript cwd wins over payload cwd")

    # Commit window: an old session must not list commits made after it, and
    # must not report today's dirty state. Timestamps are ...000Z, which git
    # cannot parse unless normalised — that bug silently emptied this section.
    base_old = dict(cwd=str(repo), sessionId="old1", isSidechain=False)
    rows_old = [
        {"type": "user", "message": {"role": "user", "content": "3월 세션"}, "timestamp": "2026-03-10T02:00:00.000Z"},
        {"type": "user", "message": {"role": "user", "content": "p2"}, "timestamp": "2026-03-10T04:00:00.000Z"},
    ]
    (repo / "a.txt").write_text("dirty now")
    f_old = jsonl(tmp / "old.jsonl", rows_old, base_old)
    out3 = run_capture("--transcript", str(f_old), "--cwd", str(repo), "--session-id", "old1", "--stdout").stdout
    check("inside window" in out3, "in-window commit captured (git date parsing)")
    check("outside window" not in out3, "post-session commits excluded")
    check("Uncommitted" not in out3, "stale dirty state omitted for old sessions")


def test_vault_workflow(tmp: Path) -> None:
    print("\n[vault workflow: init/dedup/done/exclude]")
    home = tmp / "home"
    (home / ".claude").mkdir(parents=True)
    vault = home / "V"
    (vault / ".obsidian").mkdir(parents=True)
    r = run_util("init", "--vault", str(vault), "--exclude", "/work/private", home=home)
    check(json.loads(r.stdout)["ok"], "init creates config + folders")

    raw = vault / "DevJournal" / "_raw"
    base = dict(cwd="/repo/d", sessionId="d1", isSidechain=False)
    rich = jsonl(
        tmp / "rich.jsonl",
        [{"type": "user", "message": {"role": "user", "content": f"p{i}"}, "timestamp": ts(30 - i)} for i in range(6)]
        + [
            {
                "type": "assistant",
                "message": {"role": "assistant", "model": "m", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Edit", "input": {"file_path": f"/repo/d/{i}.py"}}]},
                "timestamp": ts(20 - i),
            }
            for i in range(6)
        ],
        base,
    )
    thin = jsonl(
        tmp / "thin2.jsonl",
        [
            {"type": "user", "message": {"role": "user", "content": "얇음 1"}, "timestamp": ts(30)},
            {"type": "user", "message": {"role": "user", "content": "얇음 2"}, "timestamp": ts(29)},
        ],
        base,
    )

    run_capture("--transcript", str(rich), "--cwd", "/repo/d", "--session-id", "d1", home=home)
    digests = list(raw.glob("*d1.md"))
    check(len(digests) == 1, "digest written to vault")

    # PreCompact/SessionEnd overlap: a thinner capture must not win.
    run_capture("--transcript", str(thin), "--cwd", "/repo/d", "--session-id", "d1", home=home)
    check("turns: 6" in digests[0].read_text(), "richer digest survives thinner re-capture")
    check(not list(raw.glob(".*tmp*")), "no temp files left behind (atomic replace)")

    # done() must only touch the frontmatter block.
    r = run_util("done", str(digests[0]), home=home)
    check("status: processed" in digests[0].read_text()[:400], "done flips frontmatter status")
    r2 = json.loads(run_util("done", str(digests[0]), home=home).stdout)
    check(len(r2["already_processed"]) == 1, "done is idempotent")

    # A processed digest must never be clobbered by a later capture.
    run_capture("--transcript", str(rich), "--cwd", "/repo/d", "--session-id", "d1", home=home)
    check("status: processed" in digests[0].read_text()[:400], "synthesised digest not overwritten")

    # A file without frontmatter must be skipped, not corrupted.
    bad = tmp / "nofm.md"
    bad.write_text("no frontmatter\n---\nbody rule\n", encoding="utf-8")
    run_util("done", str(bad), home=home)
    check(bad.read_text().startswith("no frontmatter"), "frontmatter-less file untouched")

    # exclude_paths must be enforced against the transcript's cwd too.
    excl = jsonl(
        tmp / "excl.jsonl",
        [
            {"type": "user", "message": {"role": "user", "content": "비밀"}, "timestamp": ts(5)},
            {"type": "user", "message": {"role": "user", "content": "p2"}, "timestamp": ts(4)},
        ],
        dict(cwd="/work/private/app", sessionId="x1", isSidechain=False),
    )
    out = run_capture("--transcript", str(excl), "--cwd", "/innocent/path", "--session-id", "x1", home=home).stdout
    check("exclude_paths" in out, "excluded repo skipped even when session ends elsewhere")
    check(not list(raw.glob("*x1.md")), "no digest written for excluded repo")

    # doctor / pending / stats must not crash and must see the install.
    d = json.loads(run_util("doctor", home=home).stdout)
    check(d["vault_ok"] and d["hooks_json_present"], "doctor reports healthy install")
    check(json.loads(run_util("pending", home=home).stdout)["count"] >= 0, "pending works")
    check("_raw_by_status" in json.loads(run_util("stats", home=home).stdout), "stats works")


def test_perf(tmp: Path) -> None:
    print("\n[performance]")
    big = tmp / "big.jsonl"
    base = dict(cwd="/repo/big", sessionId="big1", isSidechain=False)
    with big.open("w", encoding="utf-8") as fh:
        for i in range(4000):
            fh.write(json.dumps({**base, "type": "user", "message": {"role": "user", "content": f"p{i} " + "x" * 2000}, "timestamp": ts(60)}) + "\n")
            fh.write(json.dumps({**base, "type": "assistant", "message": {"role": "assistant", "model": "m", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Edit", "input": {"file_path": f"/repo/big/f{i%200}.py"}}]}, "timestamp": ts(30)}) + "\n")
    mb = big.stat().st_size / 1e6
    start = time.time()
    r = run_capture("--transcript", str(big), "--cwd", "/repo/big", "--session-id", "big1", "--stdout")
    elapsed = time.time() - start
    check(elapsed < 5.0, f"{mb:.0f}MB transcript parsed in {elapsed:.2f}s (< 5s)")
    check(len(r.stdout) < 20000, "digest stays small regardless of transcript size")


def main() -> int:
    m = load_capture()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_redaction(m)
        test_hostile_input(tmp)
        test_extraction(tmp)
        test_thresholds(tmp)
        test_worktree_and_git(tmp)
        test_vault_workflow(tmp)
        test_perf(tmp)

    print(f"\n{PASSES} passed, {len(FAILURES)} failed")
    for f in FAILURES:
        print(f"  FAILED: {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
