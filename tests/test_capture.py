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

# Default HOME for any subprocess that does not ask for a specific one. Set in
# main(); a module-level placeholder keeps import-time use from silently
# pointing at the real home.
SCRATCH_HOME = Path(tempfile.gettempdir()) / "obsidian-dev-journal-tests-unset"


def check(cond: bool, label: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  PASS  {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL  {label}")


def load_util():
    spec = importlib.util.spec_from_file_location("journal_util", UTIL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_capture():
    spec = importlib.util.spec_from_file_location("capture_session", CAPTURE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sandbox_env(home: Path | None) -> dict:
    """Never let a test reach the developer's real vault.

    This is not hypothetical: a `run_capture(...)` call without `home=` inherited
    the real HOME, read the real config, and wrote
    `_raw/2026-08-13--obsidian-dev-journal--garbage.md` (session_id "garbage",
    a hostile-input fixture) into the actual Obsidian vault, where it was then
    synthesised into a note. HOME alone was not enough either, because
    OBSIDIAN_VAULT and OBSIDIAN_DEV_JOURNAL_CONFIG override it.

    With no explicit home, everything is pointed at a throwaway directory that
    is asserted-clean by test_no_real_vault_writes().
    """
    home = home or SCRATCH_HOME
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["OBSIDIAN_DEV_JOURNAL_CONFIG"] = str(home / ".claude" / "obsidian-dev-journal" / "config.json")
    env.pop("OBSIDIAN_VAULT", None)
    return env


def run_capture(*args: str, home: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CAPTURE), *args],
        capture_output=True,
        text=True,
        env=sandbox_env(home),
    )


def run_util(*args: str, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(UTIL), *args],
        capture_output=True,
        text=True,
        env=sandbox_env(home),
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
        ("credentials: hunter2", "hunter2"),                        # plural keyword
        # A quoted flag value may contain spaces. The value pattern stopped at
        # the first space, so everything after it stayed in the digest.
        ("aws --secret-key 'abc def ghi'", "def"),
        ('deploy --api-key "part1 part2"', "part2"),
        ('curl --token "abc', "abc"),                               # unterminated quote
        # False positives: these must pass through untouched.
        ("git log --author=handawoon --oneline", None),
        ('git commit --author="Han <h@x.io>"', None),
        ("sort -u file.txt", None),
        ("curl -u admin http://x", None),
        ("just normal text with no secrets", None),
        # Observed in the real vault: masking that destroyed content instead of
        # secrets. A keyword that is only the prefix of a longer lowercase word
        # is a different word.
        ("master-2에서 ssh-keygen -R 10.0.0.5 실행", None),          # "-keygen" is not a flag
        ('{"authorizedStoreSnapshot":"abc123"}', None),
        ("authentication: JWT 로 바꿨다", None),
        ("the secretary: bob", None),
        ("tokenizer: sentencepiece", None),
        # Qualified names. The keyword is a namespace component or the head of
        # a compound type, never the key being assigned. The vault carried
        # `com.digitalasset.auth.TokenService: «redacted»`, where the masked
        # word was the error message.
        ("com.digitalasset.auth.TokenService: error message here", None),
        ("org.springframework.security.AuthManager: init failed", None),
        ("io.jsonwebtoken.JwtParser: malformed input", None),
        ("com.acme.auth.TokenRefresher: retry scheduled", None),
        ("secrets.json: no such file", None),          # bridge crosses a dot
        # ...and the rejection must not open a hole. When the keyword *is* the
        # whole last segment it still names the key, however deep the path.
        ("vault.secrets.token = hunter2", "hunter2"),
        ("a.b.c.d.password=hunter2", "hunter2"),
        ("config.api_key = hunter2", "hunter2"),       # one segment deep
        ("settings.token=hunter2", "hunter2"),
        ('{"secretValue": "hunter2"}', "hunter2"),     # camelCase key, not dotted
    ]
    for text, leak in cases:
        got = m.redact(text)
        if leak is None:
            check(got == text, f"untouched: {text[:44]}")
        else:
            check(leak not in got, f"redacted:  {text[:44]}")

    # Absence of the secret is not enough — a substitution can mangle the line
    # around it and every case above only asserts that the value is gone. That
    # gap shipped a `--password None«redacted»` (wrong capture group) once.
    exact = [
        ("aws --secret-key 'abc def'", "aws --secret-key '«redacted»'"),
        ("mysql --password hunter2 -u root", "mysql --password «redacted» -u root"),
        ('curl --api-key "abc123" https://x', 'curl --api-key "«redacted»" https://x'),
        ("export API_KEY=abc123 && ./run", "export API_KEY=«redacted» && ./run"),
    ]
    for text, want in exact:
        check(m.redact(text) == want, "exact form: " + want)

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


def test_prompt_channels(m, tmp: Path) -> None:
    """Queued input is recorded twice and harness text lands in the user channel.

    Backfilling 130 digests showed only 51% of captured prompts were real user
    turns: 26% were the transcript's double-write of `queue-operation` entries,
    21% were injected <task-notification> blocks, and `system` recaps were being
    quoted back as if the user had said them. That crowded genuine prompts out
    of the max_prompts budget and let 1-turn sessions clear min_user_prompts.
    """
    print("\n[prompt channels]")

    # -- dedupe_prompts() unit rules -------------------------------------- #
    check(
        m.dedupe_prompts([("shadow", "a"), ("user", "a")]) == ["a"],
        "shadow dropped when the same text arrives as a real user prompt",
    )
    check(
        m.dedupe_prompts([("user", "a"), ("shadow", "a")]) == ["a"],
        "shadow dropped regardless of which side lands first",
    )
    check(
        m.dedupe_prompts([("shadow", "q"), ("shadow", "q")]) == ["q"],
        "queued-then-delivered shadow pair collapses to one",
    )
    check(
        m.dedupe_prompts([("shadow", "A"), ("shadow", "B"), ("shadow", "A"), ("shadow", "B")])
        == ["A", "B"],
        "interleaved shadow pairs collapse (not just adjacent ones)",
    )
    # 94 real prompts in the local corpus existed *only* as queue-operation.
    check(
        m.dedupe_prompts([("shadow", "queued only")]) == ["queued only"],
        "queue-only input is kept — dropping the channel would lose real prompts",
    )
    # Genuine repetition is two turns, not a duplicate.
    check(
        m.dedupe_prompts([("user", "다시"), ("user", "다시")]) == ["다시", "다시"],
        "repeated user prompts are never deduped",
    )
    check(
        m.dedupe_prompts([("user", "1"), ("shadow", "2"), ("user", "3")]) == ["1", "2", "3"],
        "chronological order preserved across channels",
    )

    # -- end-to-end through the parser ------------------------------------ #
    base = dict(cwd="/repo/ch", sessionId="ch1", isSidechain=False)
    rows = [
        {"type": "user", "message": {"role": "user", "content": "진짜 프롬프트"}, "timestamp": ts(9)},
        # queued while busy: written once on queue, once on delivery
        {"type": "queue-operation", "message": {"role": "user", "content": "큐에 넣은 말"}, "timestamp": ts(8)},
        {"type": "queue-operation", "message": {"role": "user", "content": "큐에 넣은 말"}, "timestamp": ts(8)},
        # shadow of a prompt that also lands as a real user entry
        {"type": "queue-operation", "message": {"role": "user", "content": "그림자"}, "timestamp": ts(7)},
        {"type": "user", "message": {"role": "user", "content": "그림자"}, "timestamp": ts(7)},
        # harness-injected noise
        {"type": "user", "message": {"role": "user", "content": "<task-notification>\n<task-id>x</task-id>"}, "timestamp": ts(6)},
        {"type": "queue-operation", "message": {"role": "user", "content": "<task-notification>\n<task-id>y</task-id>"}, "timestamp": ts(6)},
        {"type": "user", "message": {"role": "user", "content": "You have 3 orchestration messages. Run `orca orchestration check`."}, "timestamp": ts(5)},
        # harness recap must not be quoted back as a user prompt
        {"type": "system", "message": {"role": "user", "content": "Goal was X. Next: Y. (disable recaps in /config)"}, "timestamp": ts(4)},
        {
            "type": "assistant",
            "message": {"role": "assistant", "model": "m", "content": [
                {"type": "tool_use", "id": "t", "name": "Edit", "input": {"file_path": "/repo/ch/a.py"}}]},
            "timestamp": ts(3),
        },
    ]
    f = jsonl(tmp / "channels.jsonl", rows, base)
    out = run_capture("--transcript", str(f), "--cwd", "/repo/ch", "--session-id", "ch1", "--stdout").stdout
    body = out.split("## What I asked for", 1)[1].split("\n## ", 1)[0]

    check("진짜 프롬프트" in body, "real user prompt kept")
    check(body.count("큐에 넣은 말") == 1, "double-written queued prompt appears once")
    check(body.count("그림자") == 1, "shadow of a delivered prompt appears once")
    check("task-notification" not in body, "<task-notification> filtered from prompts")
    check("orchestration check" not in body, "orchestration nudge filtered from prompts")
    check("Goal was X" not in body, "system recap not quoted as a user prompt")

    # Recaps are the densest line in most sessions — the backfill leaned on them
    # for "무엇을 했나" / "남은 일". Filtering them out of prompts must not throw
    # them away; they move to their own, correctly attributed section.
    check("## Session recaps" in out, "recap section emitted")
    check("Goal was X. Next: Y." in out, "recap text preserved under its own heading")
    check(
        out.index("## What I asked for") < out.index("## Session recaps"),
        "recaps rendered after prompts, before the tail sections that truncation eats",
    )

    # A commit-message-generation call is one prompt recorded on both channels;
    # before the fix it counted as 2 and cleared min_user_prompts, producing 6
    # content-free digests in the local corpus.
    hook = "You are generating a single git commit message."
    f2 = jsonl(
        tmp / "commitmsg.jsonl",
        [
            {"type": "queue-operation", "message": {"role": "user", "content": hook}, "timestamp": ts(4)},
            {"type": "user", "message": {"role": "user", "content": hook}, "timestamp": ts(4)},
        ],
        dict(cwd="/repo/cm", sessionId="cm1", isSidechain=False),
    )
    r = run_capture("--transcript", str(f2), "--cwd", "/repo/cm", "--session-id", "cm1")
    check("below signal threshold" in r.stdout, "shadow no longer inflates a 1-prompt session past the threshold")


def test_size_budget(m, tmp: Path) -> None:
    """The size cap must spend the budget on facts, not on verbose prompts.

    The old build_digest rendered every section then hard-cut the tail. Over 130
    backfilled digests, 20 hit the cap and lost exactly the densest sections:
    commits 20/20, errors 17/20, commands 8/20 — while 40 prompts at 600 chars
    each survived. Anything in this test that regresses means the synthesiser
    stops seeing what actually changed in a big session.
    """
    print("\n[size budget]")

    # -- fit_sections() unit rules ---------------------------------------- #
    def sec(name, items, title=None):
        return m._section(name, title or f"## {name}", list(items), "…{n} more")

    protected = sec("errors", [f"- err {i}" for i in range(10)])
    hog = sec("prompts", [f"{i}. " + "x" * 200 for i in range(40)])
    out = m.fit_sections(["head"], [hog, protected], 3000)
    text = "\n".join(out)
    check(len(text) <= 3000, "fitter respects the budget")
    check(all(f"- err {i}" in text for i in range(10)), "errors never trimmed to make room")
    check("## prompts" in text and "more" in text, "trimmed section keeps its heading + marker")

    # Floors stop a section from vanishing entirely.
    only_hog = sec("prompts", [f"{i}. " + "y" * 400 for i in range(40)])
    m.fit_sections(["head"], [only_hog], 500)
    check(len(only_hog["items"]) == 8, "prompts stop shrinking at their floor (8)")

    # keep_edges: first and last prompts survive, the middle goes.
    edged = sec("prompts", [f"P{i} " + "z" * 300 for i in range(20)])
    m.fit_sections(["head"], [edged], 1500)
    kept = edged["items"]
    check(kept[0].startswith("P0"), "first prompt kept (says what the session was for)")
    check(kept[-1].startswith("P19"), "last prompt kept (says how it ended)")
    check(len(kept) < 20 and edged["dropped"] > 0, "middle prompts dropped and counted")

    # Cheap sections give way before expensive ones. Sized so that emptying
    # research alone (~600 chars) closes the gap without touching prompts.
    research = sec("research", ["- https://example.com/" + "u" * 40 for _ in range(10)])
    prompts = sec("prompts", [f"{i}. " + "w" * 100 for i in range(12)])
    m.fit_sections(["head"], [prompts, research], 1500)
    check(
        research["dropped"] > 0 and prompts["dropped"] == 0,
        "research trimmed before prompts (TRIM_PLAN order honoured)",
    )
    # Trim only as much as the budget demands — not the whole section.
    partial = sec("research", ["- " + "u" * 80 for _ in range(10)])
    m.fit_sections(["head"], [partial], 200)
    check(
        0 < len(partial["items"]) < 10,
        "fitter stops at the first fit instead of emptying the section",
    )
    # Under a brutal budget it does empty — and must still leave a trace, so
    # "10 lookups, none shown" is distinguishable from "no lookups".
    gone = sec("research", ["- " + "u" * 80 for _ in range(10)])
    text_gone = "\n".join(m.fit_sections(["head"], [gone], 50))
    check(not gone["items"], "cheap section can be emptied when the budget demands it")
    check("## research" in text_gone and "10 more" in text_gone,
          "emptied section still reports what it dropped")

    # The "…N more" marker a section gains on its first drop grows the digest.
    # Tracking size as "freed = len(item)" ignored that, so the loop believed it
    # had fit while the real text was still over budget — and the tail (commits)
    # got blind-cut anyway. Pin the invariant: whatever fit_sections returns is
    # within budget, marker included.
    def wide_sections():
        return [
            sec("prompts", [f"{i}. " + "p" * 120 for i in range(20)]),
            sec("commands", [f"- cmd {i} " + "c" * 60 for i in range(20)]),
            sec("research", [f"- url {i} " + "r" * 50 for i in range(20)]),
            sec("errors", [f"- err {i} " + "e" * 70 for i in range(20)]),
        ]

    for budget in (2500, 4000, 8000):
        got = "\n".join(m.fit_sections(["head"], wide_sections(), budget))
        check(len(got) <= budget, f"fitter output within budget after markers (budget={budget})")

    # Below the sum of the floors the budget is unreachable by design; the
    # fitter shrinks to the floors and build_digest's backstop does the rest.
    floored = wide_sections()
    m.fit_sections(["head"], floored, 100)
    at_floor = {s["name"]: len(s["items"]) for s in floored}
    check(
        at_floor["prompts"] == 8 and at_floor["errors"] == 5 and at_floor["research"] == 0,
        "impossible budget shrinks every section to its floor, no further",
    )

    # -- end-to-end: a session that used to lose its tail ------------------ #
    base = dict(cwd="/repo/big2", sessionId="bg2", isSidechain=False)
    rows = []
    for i in range(40):
        rows.append({"type": "user", "message": {"role": "user", "content": f"프롬프트 {i} " + "가" * 550}, "timestamp": ts(60 - i)})
    rows.append({
        "type": "assistant",
        "message": {"role": "assistant", "model": "m", "content": [
            {"type": "tool_use", "id": "q1", "name": "AskUserQuestion",
             "input": {"questions": [{"question": "저장소?", "options": [{"label": "Redis"}]}]}}]},
        "timestamp": ts(10),
    })
    rows.append({
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "q1", "content": 'The user answered: "저장소?"="Redis"'}]},
        "timestamp": ts(9),
    })
    rows.append({
        "type": "assistant",
        "message": {"role": "assistant", "model": "m", "content": [
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "pytest -x tests/"}}]},
        "timestamp": ts(8),
    })
    rows.append({
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "b1", "content": "Error: boom", "is_error": True}]},
        "timestamp": ts(7),
    })
    for i in range(30):
        rows.append({
            "type": "assistant",
            "message": {"role": "assistant", "model": "m", "content": [
                {"type": "tool_use", "id": f"e{i}", "name": "Edit", "input": {"file_path": f"/repo/big2/src/mod{i}.py"}}]},
            "timestamp": ts(6),
        })
    f = jsonl(tmp / "budget.jsonl", rows, base)
    out2 = run_capture("--transcript", str(f), "--cwd", "/repo/big2", "--session-id", "bg2", "--stdout").stdout

    check(len(out2) <= 14000, "digest still respects max_digest_chars")
    check("## Files touched" in out2 and "mod0.py" in out2, "files survive a prompt-heavy session")
    check("## Errors" in out2 and "boom" in out2, "errors survive a prompt-heavy session")
    check("**chose:** Redis" in out2, "AskUserQuestion survives a prompt-heavy session")
    check("pytest -x" in out2, "commands survive a prompt-heavy session")
    check("_[digest truncated]_" not in out2, "no blind tail cut — sections shrink instead")
    check("프롬프트 0" in out2, "opening prompt survives")


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


def test_repo_attribution(m, tmp: Path) -> None:
    """Which repo a session belongs to. Every case here was wrong in the vault."""
    print("\n[repo attribution]")
    if not shutil.which("git"):
        print("  SKIP  git not available")
        return

    root = tmp / "attr"
    repo = root / "my-api"
    (repo / "tests" / "unit").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], capture_output=True)

    # `git -C <dir> rev-parse --git-common-dir` answers relative to <dir>. It
    # used to be resolved against the worktree *root*, so from my-api/tests git
    # said "../.git" and the plugin read ~/attr/.git — repo "attr", worktree
    # "my-api", repo_name_guessed false. 11 of 133 real digests were filed that
    # way, two of them with no warning at all.
    for depth, where in ((1, repo / "tests"), (2, repo / "tests" / "unit")):
        ident = m.repo_identity(str(where))
        check(ident["repo"] == "my-api", f"subdirectory (depth {depth}) attributed to its repo")
        check(ident["worktree"] == "", f"subdirectory (depth {depth}) is not a phantom worktree")
        check(ident["guessed"] is False, f"subdirectory (depth {depth}) is not a guess")

    # The repo root itself and a real linked worktree must be unaffected.
    check(m.repo_identity(str(repo))["repo"] == "my-api", "repo root still resolves")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t.io", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"], capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(root / "feat-y"), "-b", "feat-y"],
        capture_output=True,
    )
    wt = m.repo_identity(str(root / "feat-y"))
    check(wt["repo"] == "my-api" and wt["worktree"] == "feat-y", "linked worktree unchanged by the fix")

    # A deleted checkout walks up for a surviving repo, but only a few levels:
    # an unbounded climb lands on ~ , and a dotfiles-managed home is a git repo,
    # so every removed worktree would confidently claim the user's home.
    deep = repo / "a" / "b" / "c" / "d" / "e" / "gone"
    far = m.repo_identity(str(deep))
    check(far["guessed"] is True, "a checkout too deep under a repo stays a guess")
    check("no longer exists" in far["reason"], "and says the directory is gone")

    # Which cwd *is* the session. It used to be the last one recorded, so a
    # session that ended with one `cd tests/` was filed under whatever that
    # resolved to — while journal_util read the first cwd and the note's date
    # came from the first timestamp. 16 of 218 transcripts have first != last.
    check(m.resolve_cwd({}) is None, "no cwd recorded -> None")
    check(m.resolve_cwd({"/a": 40, "/b": 2}) == "/a", "the dominant cwd wins over a late cd")
    check(m.resolve_cwd({"/a": 3, "/b": 3}) == "/a", "ties go to the earliest cwd")

    # Without git to canonicalise, the leaf directory is a bad project name:
    # `<project>/scripts` was filed as repo "scripts", `<project>/outputs` as
    # "outputs". Back off to the shallowest directory the session actually
    # visited — but only when the name is already a guess, and never to a
    # directory the session never saw.
    plain = tmp / "attr" / "not-a-repo"
    (plain / "scripts").mkdir(parents=True)
    seen = {str(plain): 3, str(plain / "scripts"): 9}
    check(m.repo_identity(str(plain / "scripts"))["repo"] == "scripts", "leaf name alone is the guess")
    check(m.refine_identity(str(plain / "scripts"), seen)["repo"] == "not-a-repo",
          "a guessed name backs off to the project directory the session worked in")
    check(m.refine_identity(str(plain / "scripts"), {})["repo"] == "scripts",
          "with no other directory observed, the leaf name stands")
    check(m.refine_identity(str(repo / "tests"), {str(root): 5})["repo"] == "my-api",
          "a resolved repo is never demoted to an ancestor")

    # The most-visited directory can be a plain folder that merely *contains*
    # repos: a session sat in ~/Work/GPU-cluster (not a repo) while doing its
    # work in .../ansible_boundless (a repo). The repo has to win.
    holder = tmp / "attr" / "holder"
    inner = holder / "real-repo"
    inner.mkdir(parents=True)
    subprocess.run(["git", "-C", str(inner), "init", "-q"], capture_output=True)
    check(m.refine_identity(str(holder), {str(holder): 30, str(inner): 4})["repo"] == "real-repo",
          "a guessed parent yields to a repo the session actually worked in")
    check(m.refine_identity(str(holder), {str(holder): 30})["repo"] == "holder",
          "but only to a directory the session really visited")

    rows = [{"cwd": str(repo), "type": "user", "message": {"role": "user", "content": f"p{i}"},
             "timestamp": ts(20 - i)} for i in range(6)]
    rows.append({"cwd": str(repo / "tests"), "type": "user",
                 "message": {"role": "user", "content": "마지막에 cd"}, "timestamp": ts(1)})
    f = jsonl(tmp / "attr.jsonl", rows, {"sessionId": "at1", "isSidechain": False})
    out = run_capture("--transcript", str(f), "--cwd", "/ignored", "--session-id", "at1", "--stdout").stdout
    check(f'cwd: "{repo}"' in out, "session cwd is where it spent its entries, not where it ended")
    check('repo: "my-api"' in out, "and the repo follows from it")


def test_command_and_failure_capture(tmp: Path) -> None:
    """Shell work that the digest used to throw away."""
    print("\n[commands + failures]")
    ok = {"content": "done", "type": "tool_result", "tool_use_id": "t%d"}

    def bash(i: int, cmd: str, result: str, is_error: bool = False) -> list:
        return [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": cmd}}]},
             "timestamp": ts(30 - i)},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}", "content": result,
                 **({"is_error": True} if is_error else {})}]},
             "timestamp": ts(29 - i)},
        ]

    rows = [{"type": "user", "message": {"role": "user", "content": "배포 확인해줘"}, "timestamp": ts(40)},
            {"type": "user", "message": {"role": "user", "content": "고쳐줘"}, "timestamp": ts(39)}]
    # P4: ops verbs are work. 6027 of 8706 bash calls were dropped as "noise",
    # led by ssh (1242) — and fleet work *is* ssh.
    rows += bash(1, "ssh master-2 'systemctl status validator'", "active (running)")
    rows += bash(2, "curl -s http://localhost:9090/-/healthy", "OK")
    rows += bash(3, "python3 scripts/report.py", "wrote report")
    rows += bash(4, "ls -la /tmp", "a b c")               # still noise
    # P3: a failing command comes back as an ordinary result. 243 such failures
    # were invisible while 22 digests with 50+ turns claimed errors_hit: 0.
    rows += bash(5, "pytest tests/", "..F.. 5 failed, 263 passed in 12s")
    rows += bash(6, "python3 -c 'import x'", "Traceback (most recent call last): ModuleNotFoundError: No module named 'x'")
    rows += bash(7, "cat notes.txt", "everything is fine, 0 failed")   # must not fire
    rows += bash(10, "dpm test", "exit=0 quiet ok: 277 failed: 0")     # count belongs to "ok:"
    rows += bash(8, "make build", "boom", True)                        # real tool error

    f = jsonl(tmp / "cmds.jsonl", rows, {"cwd": "/repo/ops", "sessionId": "cm2", "isSidechain": False})
    out = run_capture("--transcript", str(f), "--cwd", "/repo/ops", "--session-id", "cm2", "--stdout").stdout

    check("ssh master-2" in out, "ssh captured")
    check("systemctl status" in out, "systemctl captured")
    check("curl -s http" in out, "curl captured")
    check("python3 scripts/report.py" in out, "python captured")
    check("ls -la /tmp" not in out, "plain ls still filtered out")

    check("5 failed, 263 passed" in out, "a failing test run is recorded as a failure")
    check("ModuleNotFoundError" in out, "a traceback is recorded as a failure")
    check("_(inferred from output)_" in out, "inferred failures are marked as such")
    check("everything is fine" not in out, "'0 failed' does not count as a failure")
    check("quiet ok: 277" not in out, "'ok: 277 failed: 0' is a summary, not a failure")
    check("- **Bash**: boom" in out or "**Bash** on" in out, "flagged tool errors still captured")
    check("errors_hit: 3" in out, "errors_hit counts real + inferred, not the false positive")

    # An `extra_commands` regex covers domain tooling an allowlist cannot know.
    cfg = tmp / "extra.json"
    cfg.write_text(json.dumps({"extra_commands": r"\b(daml|vast)\b"}), encoding="utf-8")
    rows2 = rows + bash(9, "daml build --all", "compiled")
    f2 = jsonl(tmp / "cmds2.jsonl", rows2, {"cwd": "/repo/ops", "sessionId": "cm3", "isSidechain": False})
    env = dict(os.environ, OBSIDIAN_DEV_JOURNAL_CONFIG=str(cfg), HOME=str(SCRATCH_HOME))
    got = subprocess.run(
        [sys.executable, str(CAPTURE), "--transcript", str(f2), "--cwd", "/repo/ops",
         "--session-id", "cm3", "--stdout"], capture_output=True, text=True, env=env,
    ).stdout
    check("daml build --all" in got, "extra_commands widens the allowlist")

    bad = tmp / "badre.json"
    bad.write_text(json.dumps({"extra_commands": "("}), encoding="utf-8")
    env["OBSIDIAN_DEV_JOURNAL_CONFIG"] = str(bad)
    r = subprocess.run(
        [sys.executable, str(CAPTURE), "--transcript", str(f2), "--cwd", "/repo/ops",
         "--session-id", "cm4", "--stdout"], capture_output=True, text=True, env=env,
    )
    check(r.returncode == 0 and "ssh master-2" in r.stdout, "a broken extra_commands regex degrades, not crashes")


def test_part_digest(tmp: Path) -> None:
    """A session that keeps going after its digest was synthesised."""
    print("\n[part digests]")
    home = tmp / "parthome"
    vault = tmp / "partvault"
    (vault / ".obsidian").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    run_util("init", "--vault", str(vault), home=home)
    raw = vault / "DevJournal" / "_raw"

    def rows(n: int) -> list:
        out = [{"type": "user", "message": {"role": "user", "content": f"p{i}"}, "timestamp": ts(60 - i)}
               for i in range(3)]
        for i in range(n):
            out.append({"type": "assistant",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": f"turn {i}"}]},
                        "timestamp": ts(50 - i)})
        return out

    base = {"cwd": "/repo/long", "sessionId": "pc1", "isSidechain": False}
    f1 = jsonl(tmp / "pc-a.jsonl", rows(4), base)
    run_capture("--transcript", str(f1), "--cwd", "/repo/long", "--session-id", "pc1", home=home)
    first = sorted(raw.glob("*.md"))
    check(len(first) == 1, "PreCompact digest written")

    # Synthesised mid-session, then the session continues.
    run_util("done", str(first[0]), home=home)
    f2 = jsonl(tmp / "pc-b.jsonl", rows(9), base)
    run_capture("--transcript", str(f2), "--cwd", "/repo/long", "--session-id", "pc1", home=home)

    files = sorted(p.name for p in raw.glob("*.md"))
    check(len(files) == 2, "the rest of the session is not silently dropped")
    part = [p for p in raw.glob("*.md") if "--part2" in p.name]
    check(len(part) == 1, "it lands in a part2 file")
    if part:
        head = part[0].read_text(encoding="utf-8")[:900]
        check("part: 2" in head, "part is recorded in the frontmatter")
        check("turns: 9" in head, "the part digest is the richer one")
        check("Part 2 of this session" in head, "and warns the reader about the overlap")
    check("status: processed" in first[0].read_text(encoding="utf-8")[:600],
          "the already-synthesised digest is left untouched")

    # Not richer -> still nothing new. The escape hatch must not fire on every
    # duplicate capture of an already-processed session.
    f3 = jsonl(tmp / "pc-c.jsonl", rows(2), base)
    run_capture("--transcript", str(f3), "--cwd", "/repo/long", "--session-id", "pc1", home=home)
    check(len(list(raw.glob("*.md"))) == 2, "a thinner re-capture of a processed session writes nothing")


def test_session_identity_and_states(tmp: Path) -> None:
    """One session must yield one digest, and a digest must be parkable.

    Both gaps showed up in a 130-digest backfill: a session whose checkout was
    renamed mid-flight (zot -> internal-registry) landed as two digests and got
    synthesised twice, and a digest the user wanted deferred had nowhere to go —
    it sat in `pending` forever, indistinguishable from a backlog.
    """
    print("\n[session identity + states]")
    home = tmp / "home2"
    (home / ".claude").mkdir(parents=True)
    vault = home / "V2"
    (vault / ".obsidian").mkdir(parents=True)
    run_util("init", "--vault", str(vault), home=home)
    raw = vault / "DevJournal" / "_raw"

    # Same session, captured twice with the checkout renamed in between.
    sid = "abcdef01-2222-3333-4444-5555deadbeef"
    def rows(cwd, n):
        out = [{"type": "user", "message": {"role": "user", "content": f"프롬프트 {i}"},
                "cwd": cwd, "sessionId": sid, "timestamp": ts(20 - i)} for i in range(3)]
        for i in range(n):
            out.append({"type": "assistant", "cwd": cwd, "sessionId": sid,
                        "message": {"role": "assistant", "model": "m", "content": [
                            {"type": "tool_use", "id": f"t{i}", "name": "Edit",
                             "input": {"file_path": f"{cwd}/f{i}.py"}}]},
                        "timestamp": ts(5)})
        return out

    f1 = jsonl(tmp / "s_old.jsonl", rows("/work/zot", 2))
    run_capture("--transcript", str(f1), "--cwd", "/work/zot", "--session-id", sid, home=home)
    check(len(list(raw.glob("*.md"))) == 1, "first capture writes one digest")

    f2 = jsonl(tmp / "s_new.jsonl", rows("/work/internal-registry", 9))
    run_capture("--transcript", str(f2), "--cwd", "/work/internal-registry",
                "--session-id", sid, home=home)
    files = sorted(raw.glob("*.md"))
    check(len(files) == 1, "renamed checkout does not create a second digest for one session")
    check("internal-registry" in files[0].name, "the richer digest wins and keeps the new repo name")

    # A thinner re-capture must not replace the richer one.
    run_capture("--transcript", str(f1), "--cwd", "/work/zot", "--session-id", sid, home=home)
    files = sorted(raw.glob("*.md"))
    check(len(files) == 1 and "internal-registry" in files[0].name,
          "thinner re-capture neither replaces nor duplicates")

    # A different session that happens to share nothing must still land.
    other = "99999999-0000-0000-0000-000000000000"
    f3 = jsonl(tmp / "s_other.jsonl", rows("/work/zot", 2))
    run_capture("--transcript", str(f3), "--cwd", "/work/zot", "--session-id", other, home=home)
    check(len(list(raw.glob("*.md"))) == 2, "a genuinely different session still gets its own digest")

    # -- hold / unhold ----------------------------------------------------- #
    target = str(files[0])
    r = run_util("hold", target, "--reason", "레포가 다른 머신에 있음", home=home)
    check(json.loads(r.stdout)["held"] == [target], "hold parks a digest")
    pend = json.loads(run_util("pending", home=home).stdout)
    check(pend["held"] == 1, "pending reports parked digests separately")
    check(all(target != d["path"] for d in pend["digests"]), "parked digest leaves the queue")
    head = Path(target).read_text(encoding="utf-8")[:400]
    check("hold_reason:" in head and "레포가 다른" in head, "hold records why")

    r = run_util("done", target, home=home)
    check(json.loads(r.stdout)["skipped"], "done refuses to silently process a parked digest")

    r = run_util("unhold", target, home=home)
    check(json.loads(r.stdout)["returned_to_queue"] == [target], "unhold returns it to the queue")
    check(json.loads(run_util("pending", home=home).stdout)["held"] == 0, "no longer parked")

    # -- done --note back-reference ---------------------------------------- #
    note = str(vault / "DevJournal" / "Sessions" / "2026-08-13-wave.md")
    r = run_util("done", target, "--note", note, home=home)
    check(json.loads(r.stdout)["marked"] == [target], "done marks processed")
    head = Path(target).read_text(encoding="utf-8")[:500]
    check("notes: [" in head and "2026-08-13-wave.md" in head,
          "done records which note the digest produced")
    check(head.count("processed:") == 1, "re-stamping does not duplicate frontmatter keys")

    # -- index note --------------------------------------------------------- #
    idx = vault / "DevJournal" / "DevJournal.md"
    check(idx.is_file(), "init creates the vault index note")
    body = idx.read_text(encoding="utf-8")
    check("DevJournal/Sessions" in body and 'status = "held"' in body,
          "index queries the journal folder and surfaces parked digests")
    before = idx.read_text(encoding="utf-8")
    idx.write_text(before + "\n손으로 추가한 메모\n", encoding="utf-8")
    run_util("index", home=home)
    check("손으로 추가한 메모" in idx.read_text(encoding="utf-8"),
          "index does not clobber user edits without --force")
    run_util("index", "--force", home=home)
    check("손으로 추가한 메모" not in idx.read_text(encoding="utf-8"), "--force rewrites it")

    # -- batch suggestion ---------------------------------------------------- #
    for i in range(40):
        (raw / f"2026-01-{i % 28 + 1:02d}--r--x{i:04d}.md").write_text(
            f'---\nstatus: unprocessed\nsession_id: "x{i}"\nrepo: "r"\nstarted: "2026-01-01"\n---\n',
            encoding="utf-8",
        )
    pend = json.loads(run_util("pending", home=home).stdout)
    check(pend["suggested_batch"] > 8, "batch suggestion scales with a large backlog")
    check(pend["suggested_batch"] <= 16, "batch suggestion stays bounded")

    # -- prune -------------------------------------------------------------- #
    # `_raw` never emptied: 133 processed digests sat in the real vault, indexed
    # by Obsidian forever. Only synthesised ones may go, and they go *out* of
    # the vault so a vault search stops matching them.
    old = raw / "2026-01-01--r--pruneme.md"
    old.write_text('---\nstatus: processed\nprocessed: 2020-01-01\nsession_id: "old"\n---\nbody\n',
                   encoding="utf-8")
    fresh = raw / "2026-01-02--r--keepme.md"
    fresh.write_text('---\nstatus: processed\nprocessed: 2099-01-01\nsession_id: "new"\n---\nbody\n',
                     encoding="utf-8")
    parked = raw / "2026-01-03--r--parked.md"
    parked.write_text('---\nstatus: held\nsession_id: "held"\n---\nbody\n', encoding="utf-8")

    queued_before = len(json.loads(run_util("pending", home=home).stdout)["digests"])
    r = json.loads(run_util("prune", "--older-than", "30", "--dry-run", home=home).stdout)
    check(r["candidates"] == ["2026-01-01--r--pruneme.md"], "dry-run picks only the aged, processed digest")
    check(old.is_file(), "dry-run moves nothing")

    r = json.loads(run_util("prune", "--older-than", "30", home=home).stdout)
    check(not old.exists(), "prune removes the digest from the vault")
    check(fresh.is_file(), "a recently synthesised digest stays")
    check(parked.is_file(), "a parked digest is never pruned")
    archived = home / ".claude" / "obsidian-dev-journal" / "archive" / old.name
    check(archived.is_file(), "it is archived outside the vault, not destroyed")
    check(len(json.loads(run_util("pending", home=home).stdout)["digests"]) == queued_before,
          "pruning does not disturb the pending queue")


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
    # Status rewriting goes through tmp + os.replace like the capture path: an
    # in-place rewrite of a file inside a synced vault can be read half-written.
    check(not list(raw.glob(".*tmp*")), "done writes atomically and leaves no temp file")

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


def test_doctor_sees_every_copy(tmp: Path) -> None:
    """Two copies at different versions write different digests into one vault.

    The cached snapshot of this plugin lives six levels under ~/.claude/plugins
    (cache/<marketplace>/<plugin>/<version>/.claude-plugin/plugin.json) and the
    crawl stopped at five, so doctor reported one healthy install while a second
    runtime was really executing a two-versions-old copy — the exact failure the
    command exists to surface.
    """
    print("\n[doctor: multiple plugin copies]")
    home = tmp / "home_doc"
    cache = home / ".claude" / "plugins" / "cache" / "dev-journal" / "obsidian-dev-journal" / "0.1.0"
    (cache / ".claude-plugin").mkdir(parents=True)
    (cache / "hooks").mkdir()
    (cache / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "obsidian-dev-journal", "version": "0.1.0"}), encoding="utf-8"
    )
    (cache / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"obsidian-dev-journal@dev-journal": [
            {"scope": "user", "installPath": str(cache), "version": "0.1.0"}]}}),
        encoding="utf-8",
    )

    d = json.loads(run_util("doctor", home=home).stdout)
    paths = {i["path"] for i in d["plugin_installs_found"]}
    check(str(cache) in paths, "the cached copy six levels down is found")
    check(d["plugin_version_running"] == json.loads(
        (ROOT / ".claude-plugin" / "plugin.json").read_text())["version"],
        "the running copy reports the working tree's version")
    check([i["version"] for i in d["plugin_version_mismatch"]] == ["0.1.0"], "the stale copy is named as a mismatch")
    check(bool(d["version_note"]), "doctor says what to do about it")

    # A registry entry pointing at a directory that no longer exists is still a
    # runtime aimed at nothing — report it rather than silently dropping it.
    shutil.rmtree(cache)
    d2 = json.loads(run_util("doctor", home=home).stdout)
    gone = [i for i in d2["plugin_installs_found"] if i["path"] == str(cache)]
    check(len(gone) == 1 and gone[0]["exists"] is False, "a registered copy whose directory is gone is still reported")


def test_init_migration_resilience(tmp: Path) -> None:
    """A vault it cannot write to must not leave setup half-finished.

    Only the collision branch of the staged-digest migration was guarded, so an
    unwritable `_raw/` aborted init *after* it had created the folders and
    written the config — with the staged digests partly moved and no report of
    what failed.
    """
    print("\n[init migration resilience]")
    if os.geteuid() == 0:
        print("  SKIP  running as root; chmod would not deny writes")
        return
    home = tmp / "home_init"
    (home / ".claude").mkdir(parents=True)
    vault = home / "VI"
    (vault / ".obsidian").mkdir(parents=True)
    run_util("init", "--vault", str(vault), home=home)

    staged = home / ".claude" / "obsidian-dev-journal" / "staged"
    staged.mkdir(parents=True, exist_ok=True)
    digest = staged / "2026-08-13--repo--stagedig.md"
    digest.write_text('---\nstatus: unprocessed\nturns: 4\n---\n본문\n', encoding="utf-8")

    raw = vault / "DevJournal" / "_raw"
    raw.chmod(0o500)
    try:
        r = json.loads(run_util("init", "--vault", str(vault), home=home).stdout)
    finally:
        raw.chmod(0o700)
    check(r.get("ok") is True, "init survives an unwritable vault instead of crashing")
    check(len(r.get("migration_failed") or []) == 1, "init reports the digest it could not migrate")
    check(digest.is_file(), "the staged digest is left in place so a later init can retry")

    r2 = json.loads(run_util("init", "--vault", str(vault), home=home).stdout)
    check(r2["migrated_digests"] == 1 and not r2["migration_failed"], "retry migrates it once the vault is writable")


def test_host_field(m, tmp: Path) -> None:
    """Which machine a session ran on is capture-time-only information.

    The field exists for the day two machines' vaults are merged into one:
    digests are keyed by session_id and carry no other clue about their origin.
    Nothing can reconstruct it afterwards, so it has to be right from the first
    capture — and it must never *invent* a host, which is exactly what
    slugify()'s "session" fallback does when handed an empty string.
    """
    print("\n[host field]")
    check(m.host_name({"host_name": "work-mbp"}) == "work-mbp", "config host_name wins over the hostname")
    check("." not in m.host_name({}), "auto host keeps the first label only (mbp.local flips to .lan on another network)")
    check(m.host_name({"host_name": 'a"b c'}) == "a-b-c", "host is slugified, so it cannot break the YAML block")

    check(m.host_name({"host_name": "###"}) == "", "a hostname with nothing usable in it yields '', not 'session'")

    real_gethostname = m.socket.gethostname
    try:
        m.socket.gethostname = lambda: ""
        check(m.host_name({}) == "", "a machine with no hostname yields '', not slugify's 'session' fallback")
        m.socket.gethostname = lambda: (_ for _ in ()).throw(OSError("no hostname"))
        check(m.host_name({}) == "", "a failing gethostname degrades to '' instead of killing the digest")
    finally:
        m.socket.gethostname = real_gethostname

    # End to end: two machines' digests land side by side, as they would after
    # someone merges the `_raw` folders of two vaults.
    home = tmp / "home_host"
    (home / ".claude").mkdir(parents=True)
    vault = home / "VH"
    (vault / ".obsidian").mkdir(parents=True)
    run_util("init", "--vault", str(vault), home=home)
    cfg_path = home / ".claude" / "obsidian-dev-journal" / "config.json"
    raw = vault / "DevJournal" / "_raw"

    def capture_on(host: str, sid: str) -> Path:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["host_name"] = host
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        rows = [{"type": "user", "message": {"role": "user", "content": f"프롬프트 {i}"},
                 "cwd": "/repo/h", "sessionId": sid, "timestamp": ts(10 - i)} for i in range(3)]
        f = jsonl(tmp / f"host_{sid}.jsonl", rows)
        run_capture("--transcript", str(f), "--cwd", "/repo/h", "--session-id", sid, home=home)
        return next(p for p in raw.glob(f"*{sid}.md"))

    a = capture_on("work-mbp", "hosta111")
    check('host: "work-mbp"' in a.read_text()[:600], "host lands in the frontmatter block")
    b = capture_on("home-desktop", "hostb222")

    st = json.loads(run_util("stats", home=home).stdout)
    check(st.get("_raw_by_host") == {"work-mbp": 1, "home-desktop": 1}, "stats splits a merged vault by host")
    pend = json.loads(run_util("pending", home=home).stdout)
    check({d["host"] for d in pend["digests"]} == {"work-mbp", "home-desktop"}, "pending exposes host per digest")

    # The new key sits inside the YAML block, so status rewriting must still
    # find its anchor and must not disturb the line.
    run_util("done", str(b), home=home)
    text = b.read_text()[:600]
    check("status: processed" in text and 'host: "home-desktop"' in text, "done leaves host intact")


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


def test_no_real_vault_writes(tmp: Path) -> None:
    """The suite must be incapable of writing into the developer's own vault."""
    print("\n[isolation]")
    real_home = Path(os.path.expanduser("~"))
    check(SCRATCH_HOME != real_home, "scratch HOME is not the real HOME")

    env = sandbox_env(None)
    check(env["HOME"] == str(SCRATCH_HOME), "default subprocess HOME is the scratch home")
    check("OBSIDIAN_VAULT" not in env, "OBSIDIAN_VAULT is stripped from the test env")
    check(
        env["OBSIDIAN_DEV_JOURNAL_CONFIG"].startswith(str(SCRATCH_HOME)),
        "config path is redirected into the scratch home",
    )

    # A real vault configured in the developer's real config must stay untouched
    # even when capture is invoked exactly the way the leaking call did:
    # no home=, --force, and a transcript that yields an empty digest.
    real_cfg = real_home / ".claude" / "obsidian-dev-journal" / "config.json"
    before = set()
    real_raw = None
    if real_cfg.is_file():
        try:
            vault = json.loads(real_cfg.read_text(encoding="utf-8")).get("vault_path") or ""
            if vault:
                real_raw = Path(vault) / "DevJournal" / "_raw"
                before = {p.name for p in real_raw.glob("*.md")} if real_raw.is_dir() else set()
        except Exception:
            pass

    (tmp / "iso.jsonl").write_text("not json\n{\n", encoding="utf-8")
    run_capture("--transcript", str(tmp / "iso.jsonl"), "--force", "--session-id", "isolation-probe")

    # The filename only carries the last 8 chars of the session id, so identify
    # the probe by its frontmatter instead.
    wrote = [
        p
        for p in SCRATCH_HOME.rglob("*.md")
        if 'session_id: "isolation-probe"' in p.read_text(encoding="utf-8", errors="replace")[:600]
    ]
    check(bool(wrote), "the probe digest landed in the scratch home (so the write path ran)")
    if real_raw is not None and real_raw.is_dir():
        after = {p.name for p in real_raw.glob("*.md")}
        check(after == before, "the real vault gained no files")
    else:
        check(True, "no real vault configured; nothing to protect")



def test_frontmatter_safety(m, tmp: Path) -> None:
    """Frontmatter values come from outside and must never break the block."""
    print("\n[frontmatter quoting]")
    check(m.yaml_str('my"repo') == '"my\\"repo"', "a double quote is escaped, not emitted raw")
    check(m.yaml_str("a\\b") == '"a\\\\b"', "a backslash is escaped")
    check("\n" not in m.yaml_str("l1\nl2"), "a newline cannot end the scalar early")
    check(m.yaml_str(None) == '""', "None renders as an empty scalar, not 'None'")

    # A checkout named my"repo produced `repo: "my"repo"`, after which every
    # frontmatter reader — Obsidian, and journal_util's own frontmatter() —
    # misread status and session_id, so dedupe and `pending` stopped seeing it.
    work = tmp / 'fm' / 'my"repo'
    work.mkdir(parents=True)
    t = jsonl(tmp / "fm.jsonl", [
        {"type": "user", "timestamp": ts(9), "cwd": str(work),
         "message": {"role": "user", "content": "one"}},
        {"type": "user", "timestamp": ts(8), "cwd": str(work),
         "message": {"role": "user", "content": "two"}},
    ])
    out = run_capture("--transcript", str(t), "--force", "--stdout").stdout
    fm = out.split("---")[1]
    check('repo: "my\\"repo"' in fm, "a quote in the repo name is escaped in the digest")
    check(fm.count("\nsession_id:") == 1, "the frontmatter block stays well-formed")

    # journal_util must read back exactly what capture wrote.
    home = tmp / "fmhome"
    (home / ".claude").mkdir(parents=True)
    d = tmp / "fmread"
    d.mkdir()
    (d / "x.md").write_text('---\nstatus: unprocessed\nrepo: "my\\"repo"\n---\nbody\n',
                            encoding="utf-8")
    util = load_util()
    check(util.frontmatter(d / "x.md").get("repo") == 'my"repo',
          "frontmatter() unescapes what yaml_str() escaped")


def test_shell_failure_tail(tmp: Path) -> None:
    """A failure signs itself at the END of a long log."""
    print("\n[shell failure: tail of long output]")
    # 37KB of passing lines followed by "3 failed, 1197 passed". The scanner
    # used to read only the first 6000 chars, so the digest of a genuinely
    # failing test run claimed errors_hit: 0 — the exact blind spot
    # SHELL_FAILURE_RE was added to close.
    long_log = "\n".join(f"tests/test_{i}.py::case ... ok" for i in range(1200))
    t = jsonl(tmp / "tail.jsonl", [
        {"type": "user", "timestamp": ts(9), "cwd": str(tmp),
         "message": {"role": "user", "content": "run the suite"}},
        {"type": "user", "timestamp": ts(8), "cwd": str(tmp),
         "message": {"role": "user", "content": "fix it"}},
        {"type": "assistant", "timestamp": ts(7), "cwd": str(tmp), "message": {
            "role": "assistant", "model": "m",
            "content": [{"type": "tool_use", "id": "b1", "name": "Bash",
                         "input": {"command": "pytest -q"}}]}},
        {"type": "user", "timestamp": ts(6), "cwd": str(tmp), "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "b1",
                         "content": long_log + "\n3 failed, 1197 passed"}]}},
    ])
    out = run_capture("--transcript", str(t), "--force", "--stdout").stdout
    check("errors_hit: 1" in out, "a failure in the tail of a 37KB log is detected")
    check("_(inferred from output)_" in out, "it is marked inferred, not harness-reported")
    check("3 failed" in out, "the quoted detail is the failure line, not the first 320 chars")

    # ...and the head still works, so this is a widening, not a move.
    t2 = jsonl(tmp / "head.jsonl", [
        {"type": "user", "timestamp": ts(9), "cwd": str(tmp),
         "message": {"role": "user", "content": "a"}},
        {"type": "user", "timestamp": ts(8), "cwd": str(tmp),
         "message": {"role": "user", "content": "b"}},
        {"type": "assistant", "timestamp": ts(7), "cwd": str(tmp), "message": {
            "role": "assistant", "model": "m",
            "content": [{"type": "tool_use", "id": "b2", "name": "Bash",
                         "input": {"command": "./deploy"}}]}},
        {"type": "user", "timestamp": ts(6), "cwd": str(tmp), "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "b2",
                         "content": "command not found\n" + long_log}]}},
    ])
    check("errors_hit: 1" in run_capture("--transcript", str(t2), "--force", "--stdout").stdout,
          "a failure at the head of a long log is still detected")


def test_config_robustness(m, tmp: Path) -> None:
    """A hand-edited config must degrade, never take the capture down."""
    print("\n[config robustness]")
    cfg = m.coerce_config({"max_prompts": "40", "max_digest_chars": 5})
    check(cfg["max_prompts"] == 40, "a numeric string is coerced to int")
    check(cfg["max_digest_chars"] >= 500, "an unusably small budget is floored")
    check(m.coerce_config({"max_files": None})["max_files"] == m.DEFAULTS["max_files"],
          "a null falls back to the default")
    check(m.coerce_config({"exclude_paths": "/a"})["exclude_paths"] == [],
          "a stringly-typed exclude_paths is ignored rather than iterated per character")

    # Before coercion, `2 > "40"` raised TypeError deep inside build_digest and
    # the top-level guard swallowed it: the session vanished with one log line.
    home = tmp / "cfghome"
    (home / ".claude" / "obsidian-dev-journal").mkdir(parents=True)
    (home / ".claude" / "obsidian-dev-journal" / "config.json").write_text(
        json.dumps({"max_prompts": "40"}), encoding="utf-8")
    t = jsonl(tmp / "cfg.jsonl", [
        {"type": "user", "timestamp": ts(9), "cwd": str(tmp),
         "message": {"role": "user", "content": "one"}},
        {"type": "user", "timestamp": ts(8), "cwd": str(tmp),
         "message": {"role": "user", "content": "two"}},
    ])
    out = run_capture("--transcript", str(t), "--force", "--stdout", home=home).stdout
    check("type: dev-journal-raw" in out, "a stringly-typed limit still produces a digest")


def test_exclude_path_expansion(tmp: Path) -> None:
    """`~/private` in the config used to match nothing at all."""
    print("\n[exclude_paths expansion]")
    home = tmp / "exhome"
    work = home / "client-private"
    work.mkdir(parents=True)
    (home / ".claude" / "obsidian-dev-journal").mkdir(parents=True)
    (home / ".claude" / "obsidian-dev-journal" / "config.json").write_text(
        json.dumps({"exclude_paths": ["~/client-private"]}), encoding="utf-8")
    t = jsonl(tmp / "ex.jsonl", [
        {"type": "user", "timestamp": ts(9), "cwd": str(work),
         "message": {"role": "user", "content": "secret one"}},
        {"type": "user", "timestamp": ts(8), "cwd": str(work),
         "message": {"role": "user", "content": "secret two"}},
    ])
    r = run_capture("--transcript", str(t), "--force", "--stdout", home=home)
    check("type: dev-journal-raw" not in r.stdout,
          "a ~-relative exclude_paths entry actually excludes")
    check("secret one" not in r.stdout, "no prompt from an excluded repo leaks")

    # An absolute entry must keep working exactly as before.
    (home / ".claude" / "obsidian-dev-journal" / "config.json").write_text(
        json.dumps({"exclude_paths": [str(work)]}), encoding="utf-8")
    r = run_capture("--transcript", str(t), "--force", "--stdout", home=home)
    check("type: dev-journal-raw" not in r.stdout, "an absolute entry still excludes")


def test_unwritable_vault_stages(tmp: Path) -> None:
    """A vault that exists but cannot be written used to lose the session."""
    print("\n[unwritable vault]")
    home = tmp / "rohome"
    (home / ".claude" / "obsidian-dev-journal").mkdir(parents=True)
    vault = home / "V"
    raw = vault / "DevJournal" / "_raw"
    raw.mkdir(parents=True)
    (home / ".claude" / "obsidian-dev-journal" / "config.json").write_text(
        json.dumps({"vault_path": str(vault)}), encoding="utf-8")
    t = jsonl(tmp / "ro.jsonl", [
        {"type": "user", "timestamp": ts(9), "cwd": str(tmp),
         "message": {"role": "user", "content": "one"}},
        {"type": "user", "timestamp": ts(8), "cwd": str(tmp),
         "message": {"role": "user", "content": "two"}},
    ])
    os.chmod(raw, 0o500)
    try:
        run_capture("--transcript", str(t), "--force", "--session-id", "roprobe", home=home)
        staged = list((home / ".claude" / "obsidian-dev-journal" / "staged").glob("*.md"))
        check(bool(staged), "the digest is staged instead of being lost")
        check(not list(raw.glob("*")), "nothing half-written is left in the vault")
    finally:
        os.chmod(raw, 0o700)


def test_pending_is_bounded(tmp: Path) -> None:
    """`pending` is the skill's first call; its output goes straight to a model."""
    print("\n[pending output is bounded]")
    home = tmp / "bigqueue"
    (home / ".claude").mkdir(parents=True)
    vault = home / "V"
    (vault / ".obsidian").mkdir(parents=True)
    run_util("init", "--vault", str(vault), home=home)
    raw = vault / "DevJournal" / "_raw"
    for i in range(120):
        (raw / f"2026-01-{i % 28 + 1:02d}--repo--{i:08d}.md").write_text(
            '---\nstatus: unprocessed\nsession_id: "s%08d"\nrepo: "repo"\n'
            'started: "2026-01-%02d"\nturns: 3\n---\n%s\n' % (i, i % 28 + 1, "x" * 4000),
            encoding="utf-8")
    out = json.loads(run_util("pending", home=home).stdout)
    # 1667 unprocessed digests rendered 634KB of JSON — ~160k tokens spent
    # before a single note was written, on exactly the backlog this clears.
    check(out["count"] == 120, "count still reports the true total")
    check(out["returned"] <= 40, "the default page is bounded")
    check(out["truncated"] is True, "the caller is told the list was cut")
    check(out["digests"][0]["started"] <= out["digests"][-1]["started"],
          "the bounded page is the oldest digests, not an arbitrary slice")
    everything = json.loads(run_util("pending", "--limit", "0", home=home).stdout)
    check(everything["returned"] == 120, "--limit 0 still returns everything for scripting")


def test_pending_exposes_skill_fields(tmp: Path) -> None:
    """The skill has rules keyed on `part` and `repo_name_guessed`."""
    print("\n[pending exposes part / guessed]")
    home = tmp / "pf"
    (home / ".claude").mkdir(parents=True)
    vault = home / "V"
    (vault / ".obsidian").mkdir(parents=True)
    run_util("init", "--vault", str(vault), home=home)
    raw = vault / "DevJournal" / "_raw"
    (raw / "a.md").write_text(
        '---\nstatus: unprocessed\nsession_id: "a"\npart: 2\n'
        'repo_name_guessed: true\nstarted: "2026-01-01"\n---\n', encoding="utf-8")
    (raw / "b.md").write_text(
        '---\nstatus: unprocessed\nsession_id: "b"\n'
        'repo_name_guessed: false\nstarted: "2026-01-02"\n---\n', encoding="utf-8")
    items = {i["path"].rsplit("/", 1)[-1]: i
             for i in json.loads(run_util("pending", home=home).stdout)["digests"]}
    check(items["a.md"]["part"] == 2, "a part digest is flagged without opening the file")
    check(items["a.md"]["repo_name_guessed"] is True, "a guessed repo name is flagged")
    check(items["b.md"]["part"] == 0, "a normal digest reports part 0")
    check(items["b.md"]["repo_name_guessed"] is False, "a resolved repo is not flagged")


def test_backfill_unknown_cwd(tmp: Path) -> None:
    """A cwd-less transcript must not be filed under the user's home repo."""
    print("\n[backfill: unknown cwd]")
    # The fallback was str(Path.home()). A home directory under dotfiles
    # management is a git repo, so every transcript without a cwd claimed the
    # user's home as its repository.
    home = tmp / "bfhome"
    (home / ".claude" / "projects" / "p").mkdir(parents=True)
    vault = home / "V"
    (vault / ".obsidian").mkdir(parents=True)
    run_util("init", "--vault", str(vault), home=home)
    jsonl(home / ".claude" / "projects" / "p" / "nocwd.jsonl", [
        {"type": "user", "timestamp": ts(9), "message": {"role": "user", "content": "one"}},
        {"type": "user", "timestamp": ts(8), "message": {"role": "user", "content": "two"}},
    ])
    run_util("backfill", "--limit", "5", home=home)
    digests = list((vault / "DevJournal" / "_raw").glob("*.md"))
    check(bool(digests), "a cwd-less transcript is still digested")
    text = digests[0].read_text(encoding="utf-8")
    check('repo: "unknown"' in text, "it is filed as 'unknown', not as the home directory")
    check(str(home.name) not in text.split("---")[1], "the home directory name is not claimed as a repo")


def test_doctor_flags_config_mistakes(tmp: Path) -> None:
    """Config mistakes degrade capture silently; doctor is where they surface."""
    print("\n[doctor: config problems]")
    home = tmp / "dch"
    (home / ".claude" / "obsidian-dev-journal").mkdir(parents=True)
    (home / ".claude" / "obsidian-dev-journal" / "config.json").write_text(
        json.dumps({"extra_commands": "(unclosed", "max_prompts": "40",
                    "exclude_paths": ["~/private"]}), encoding="utf-8")
    rep = json.loads(run_util("doctor", home=home).stdout)
    problems = " ".join(rep["config_problems"])
    check("extra_commands" in problems, "an invalid extra_commands regex is reported")
    check("max_prompts" in problems, "a stringly-typed limit is reported")
    check(rep["exclude_paths"][0]["expands_to"].startswith(str(home)),
          "exclude_paths shows what each entry actually expands to")



def test_git_autocommit(tmp: Path) -> None:
    """`done` closes every synthesis pass, so it is where the journal gets committed.

    The vault here is one repo with the journal inside it, and something else is
    already staged — the commit must take the journal folder and nothing else.
    """
    print("\n[done: git autocommit]")
    home = tmp / "gac"
    (home / ".claude").mkdir(parents=True)
    vault = home / "V"
    (vault / ".obsidian").mkdir(parents=True)
    run_util("init", "--vault", str(vault), home=home)
    cfg_path = home / ".claude" / "obsidian-dev-journal" / "config.json"

    def set_cfg(**kw) -> None:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.update(kw)
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    def git(*a: str, cwd: Path = vault) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True,
                              env=sandbox_env(home))

    base = vault / "DevJournal"
    n = [0]

    def synthesise() -> tuple[Path, Path]:
        n[0] += 1
        digest = base / "_raw" / f"2026-10-01--repo--g{n[0]}.md"
        digest.write_text(f"---\nstatus: unprocessed\nsession_id: \"g{n[0]}\"\n---\n\nbody\n",
                          encoding="utf-8")
        note = base / "Sessions" / f"2026-10-01-repo-g{n[0]}.md"
        note.write_text("---\ntype: session\n---\n\n# g\n", encoding="utf-8")
        return digest, note

    remote = tmp / "gac-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.name", "t")
    git("config", "user.email", "t@example.com")
    (vault / "other.md").write_text("v1\n", encoding="utf-8")
    git("add", "other.md")
    git("commit", "-q", "-m", "init")
    git("remote", "add", "origin", str(remote))
    git("push", "-q", "-u", "origin", "main")
    (vault / "other.md").write_text("v2\n", encoding="utf-8")
    git("add", "other.md")  # the user's own staged work, outside the journal

    # Off by default: nothing is committed behind the user's back.
    d, note = synthesise()
    head = git("rev-parse", "HEAD").stdout
    rep = json.loads(run_util("done", str(d), "--note", str(note), home=home).stdout)
    check("git" not in rep and git("rev-parse", "HEAD").stdout == head,
          "without git_autocommit, done does not commit")

    set_cfg(git_autocommit=True)
    d, note = synthesise()
    r = run_util("done", str(d), "--note", str(note), home=home)
    rep = json.loads(r.stdout)
    g = rep.get("git", {})
    check(g.get("committed") and g.get("pushed"), "done commits and pushes the journal")
    shown = git("show", "--name-only", "--format=%s%n%b", "HEAD").stdout
    check(shown.startswith("dev-journal: synthesise 1 digest"), "commit subject counts the digests")
    check("- Sessions/2026-10-01-repo-g2.md" in shown,
          "commit body lists the notes, relative to the journal folder")
    check("DevJournal/Sessions/2026-10-01-repo-g1.md" in shown,
          "notes written before autocommit was on are swept in, not lost")
    check("other.md" not in shown, "the commit is scoped to the journal folder")
    check("other.md" in git("diff", "--cached", "--name-only").stdout,
          "the user's staged work outside the journal stays staged")
    check(git("rev-parse", "main", cwd=remote).stdout == git("rev-parse", "HEAD").stdout,
          "the remote has the commit")

    rep = json.loads(run_util("done", str(d), home=home).stdout)
    check(rep["git"].get("reason") == "nothing to commit", "a no-op done does not make an empty commit")

    # A push failure must not undo or hide the local work.
    git("remote", "set-url", "origin", str(tmp / "nowhere.git"))
    d, note = synthesise()
    r = run_util("done", str(d), "--note", str(note), home=home)
    g = json.loads(r.stdout)["git"]
    check(r.returncode == 0 and g["committed"] and g["pushed"] is False and g.get("push_error"),
          "a failed push is reported, the commit stays, done still succeeds")
    check("status: processed" in d.read_text()[:200], "digests are marked even when the push fails")

    # Doctor: autocommit that cannot do anything has to say so.
    set_cfg(git_autocommit="true")
    problems = " ".join(json.loads(run_util("doctor", home=home).stdout)["config_problems"])
    check("git_autocommit should be true or false" in problems, "a quoted boolean is reported")

    home2 = tmp / "gac2"
    (home2 / ".claude").mkdir(parents=True)
    vault2 = home2 / "V"
    (vault2 / ".obsidian").mkdir(parents=True)
    run_util("init", "--vault", str(vault2), home=home2)
    cfg2 = home2 / ".claude" / "obsidian-dev-journal" / "config.json"
    cfg = json.loads(cfg2.read_text(encoding="utf-8"))
    cfg["git_autocommit"] = True
    cfg2.write_text(json.dumps(cfg), encoding="utf-8")
    rep = json.loads(run_util("doctor", home=home2).stdout)
    check("not inside a git repository" in " ".join(rep["config_problems"]),
          "autocommit on a journal outside any repo is reported")
    d2 = vault2 / "DevJournal" / "_raw" / "x.md"
    d2.write_text("---\nstatus: unprocessed\n---\n", encoding="utf-8")
    r = run_util("done", str(d2), home=home2)
    check(r.returncode == 0 and json.loads(r.stdout)["git"]["committed"] is False,
          "done outside a repo still marks digests and does not crash")


def test_topics_map(tmp: Path) -> None:
    """Topic notes were only linked *from* sessions, so they never met."""
    print("\n[topics map]")
    home = tmp / "tp"
    (home / ".claude").mkdir(parents=True)
    vault = home / "V"
    (vault / ".obsidian").mkdir(parents=True)
    run_util("init", "--vault", str(vault), home=home)
    learn = vault / "DevJournal" / "Learnings"

    (learn / "Canton LSU.md").write_text(
        "---\ntype: learning\ntags: [learning, canton]\nlast_updated: 2026-07-30\n"
        "confidence: solid\n---\n\n# Canton LSU\n\n## 핵심\n"
        "topology freeze 는 topology 트랜잭션만 멈춘다.\n\n"
        "## 관련\n- [[Canton 온보딩]] — 같은 경로\n- [[2026-07-30-sess]]\n",
        encoding="utf-8")
    (learn / "Canton 온보딩.md").write_text(
        "---\ntype: learning\ntags: [learning, canton]\nlast_updated: 2026-07-29\n"
        "confidence: shaky\n---\n\n# Canton 온보딩\n\n## 핵심\n"
        "**온보딩**은 `party` 생성이 먼저다.\n",
        encoding="utf-8")
    (learn / "Redis 원자성.md").write_text(
        "---\ntype: learning\ntags: [learning, redis]\nlast_updated: 2026-08-01\n---\n\n"
        "# Redis 원자성\n\n## 핵심\nINCR + EXPIRE 는 원자적이 아니다.\n",
        encoding="utf-8")

    out = json.loads(run_util("topics", home=home).stdout)
    by = {t["name"]: t for t in out["topics"]}
    check(out["count"] == 3, "every topic note is listed")
    # Linking needs to know what a note *says*; reading 41 files to place one
    # link is not affordable, so the gist comes back with the name.
    check(by["Canton LSU"]["gist"].startswith("topology freeze"),
          "the ## 핵심 line is extracted as the gist")
    check("**" not in by["Canton 온보딩"]["gist"] and "`" not in by["Canton 온보딩"]["gist"],
          "the gist is stripped of markdown emphasis")
    check(by["Canton LSU"]["links_to"] == ["Canton 온보딩"],
          "only Learning→Learning links count; the session link is ignored")
    check(by["Canton 온보딩"]["linked_from"] == ["Canton LSU"], "the reverse edge is reported")
    check(out["isolated"] == ["Redis 원자성"] and out["isolated_count"] == 1,
          "a topic no other topic reaches is called out")
    check(by["Canton 온보딩"]["confidence"] == "shaky", "frontmatter fields come along")

    filtered = json.loads(run_util("topics", "--query", "redis", home=home).stdout)
    check([t["name"] for t in filtered["topics"]] == ["Redis 원자성"],
          "--query narrows the map so the whole vault need not be loaded")
    check(filtered["count"] == 1 and filtered["isolated_count"] == 1,
          "counts describe the filtered view")
    tagged = json.loads(run_util("topics", "--query", "canton", home=home).stdout)
    check(len(tagged["topics"]) == 2, "--query matches tags, not just names")
    bounded = json.loads(run_util("topics", "--limit", "1", home=home).stdout)
    check(bounded["returned"] == 1 and bounded["truncated"] is True,
          "the map is bounded like pending, for the same context reason")


def main() -> int:
    global SCRATCH_HOME
    m = load_capture()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        SCRATCH_HOME = tmp / "_scratch_home"
        (SCRATCH_HOME / ".claude" / "obsidian-dev-journal").mkdir(parents=True, exist_ok=True)
        test_no_real_vault_writes(tmp)
        test_redaction(m)
        test_hostile_input(tmp)
        test_extraction(tmp)
        test_thresholds(tmp)
        test_prompt_channels(m, tmp)
        test_size_budget(m, tmp)
        test_worktree_and_git(tmp)
        test_repo_attribution(m, tmp)
        test_command_and_failure_capture(tmp)
        test_part_digest(tmp)
        test_session_identity_and_states(tmp)
        test_vault_workflow(tmp)
        test_doctor_sees_every_copy(tmp)
        test_init_migration_resilience(tmp)
        test_host_field(m, tmp)
        test_frontmatter_safety(m, tmp)
        test_shell_failure_tail(tmp)
        test_config_robustness(m, tmp)
        test_exclude_path_expansion(tmp)
        test_unwritable_vault_stages(tmp)
        test_pending_is_bounded(tmp)
        test_pending_exposes_skill_fields(tmp)
        test_backfill_unknown_cwd(tmp)
        test_doctor_flags_config_mistakes(tmp)
        test_git_autocommit(tmp)
        test_topics_map(tmp)
        test_perf(tmp)

    print(f"\n{PASSES} passed, {len(FAILURES)} failed")
    for f in FAILURES:
        print(f"  FAILED: {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
