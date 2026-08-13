#!/usr/bin/env python3
"""Deterministic Claude Code session -> compact Obsidian digest.

Runs from the SessionEnd / PreCompact hook in a detached background process.
Never calls an LLM, so a coding session costs zero extra tokens. It reduces a
multi-megabyte transcript down to a few kilobytes of high-signal facts, which
the /dev-journal skill later synthesises in a single batched pass.

Claude Code's transcript format is internal and can change between releases, so
every field access here is defensive: unknown shapes degrade to "less detail in
the digest", never to a crash.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.path.expanduser("~/.claude/obsidian-dev-journal"))

DEFAULTS = {
    # Absolute path to the Obsidian vault. Empty -> digests are staged locally.
    "vault_path": "",
    # Folder inside the vault that this plugin owns.
    "base_folder": "DevJournal",
    # Skip sessions with less signal than this (avoids junk notes from
    # one-question sessions).
    "min_user_prompts": 2,
    "min_files_touched": 0,
    # Caps that keep a digest small.
    "max_prompts": 40,
    "max_prompt_chars": 600,
    "max_commands": 40,
    "max_files": 60,
    "max_errors": 15,
    "max_digest_chars": 14000,
    # Directories/repos to never journal (substring match on cwd).
    "exclude_paths": [],
    # Redact obvious secrets from captured commands.
    "redact": True,
}

# Bash commands worth remembering. Everything else (ls, cat, cd...) is noise.
INTERESTING_CMD = re.compile(
    r"\b(git|gh|npm|pnpm|yarn|bun|npx|make|just|task|docker|docker-compose|kubectl|helm|"
    r"terraform|tofu|ansible|pytest|jest|vitest|mocha|go|cargo|mvn|gradle|rake|bundle|"
    r"poetry|uv|pip|ruff|mypy|eslint|prettier|tsc|alembic|prisma|psql|mysql|redis-cli|"
    r"aws|gcloud|az|flyctl|vercel|wrangler|supabase|dbt|serverless|sst)\b"
)

NOISE_TOOLS = {"TodoWrite", "Read", "Grep", "Glob", "LS", "NotebookRead", "ToolSearch"}
FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "StrReplace"}

# Secret scrubbing runs on every captured prompt and command, so it must stay
# linear. Stage 1 finds a secret-ish keyword (literal alternation, cheap); stage
# 2 matches a bounded assignment tail anchored at that position. Do not merge
# these into one pattern with a leading `.*` — that backtracks quadratically and
# made large transcripts take a minute to parse.
SECRET_KEYWORD_RE = re.compile(
    r"(?i)secret|token|passwd|password|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|auth"
)
# Tail allows an optional closing quote before the separator so JSON keys work:
#   "api_key": "hunter2"   →  keyword ends at `y`, then `"`, `:`, `"value"`.
ASSIGN_TAIL_RE = re.compile(
    r"""[A-Za-z0-9_.\-]{0,32}["']?[ \t]{0,4}[=:][ \t]{0,4}["']?([^\s"',;)}\]]{1,200})"""
)
# Space-separated CLI forms: --password hunter2, -p... no, only named flags and
# `set <key> <value>` (aws configure set aws_secret_access_key XXX).
FLAG_SECRET_RE = re.compile(
    r"(?i)(--?[A-Za-z0-9-]*(?:key|token|secret|password|passwd|credential)[A-Za-z0-9-]*|"
    r"\bset\s+[A-Za-z0-9_]*(?:key|token|secret|password)[A-Za-z0-9_]*)"
    r"\s+([\"']?)([^\s\"']{1,200})"
)
BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._+/=\-]{8,})|\b(token)\s+([A-Za-z0-9._\-]{12,})")
# Credentials embedded in connection strings: postgres://user:pw@host
URL_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{1,20}://[^\s:/@]{1,64}):[^\s/@]{1,120}@")
# curl-style basic auth flags: -u admin:pw, --user admin:pw (keep the username)
USER_PW_RE = re.compile(r"(-u|--user)(\s+[\"']?)([^\s:\"']{1,64}):([^\s\"']{1,200})")
# Long high-entropy literals that look like keys regardless of variable name.
BLOB_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}|"
    r"xox[baprs]-[A-Za-z0-9\-]{10,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+)"
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = os.environ.get("OBSIDIAN_DEV_JOURNAL_CONFIG") or str(STATE_DIR / "config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if v is not None})
    except FileNotFoundError:
        pass
    except Exception as exc:  # malformed config should not kill capture
        log(f"config unreadable ({exc}); using defaults")

    env_vault = os.environ.get("OBSIDIAN_VAULT")
    if not cfg.get("vault_path") and env_vault:
        cfg["vault_path"] = env_vault
    return cfg


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …[truncated]"


def one_line(text: str, limit: int) -> str:
    return truncate(re.sub(r"\s+", " ", text or ""), limit)


def redact(text: str, enabled: bool = True) -> str:
    """Best-effort secret scrubbing. Not a guarantee — see exclude_paths."""
    if not enabled or not text:
        return text
    # Bearer/Basic first: the keyword-assignment stage below would otherwise
    # consume the scheme word ("Basic") as the value and leave the actual
    # credential after it intact.
    text = BEARER_RE.sub(lambda m: f"{m.group(1) or m.group(3)} «redacted»", text)
    out: list[str] = []
    pos = 0
    for kw in SECRET_KEYWORD_RE.finditer(text):
        # "auth" also matches inside "author"/"authored" — a common git flag,
        # never a secret. Check the full identifier the keyword sits in.
        ident_end = kw.end()
        while ident_end < len(text) and (text[ident_end].isalnum() or text[ident_end] in "_"):
            ident_end += 1
        ident_start = kw.start()
        while ident_start > 0 and (text[ident_start - 1].isalnum() or text[ident_start - 1] in "_"):
            ident_start -= 1
        if text[ident_start:ident_end].lower() in ("author", "authors", "authored", "coauthor"):
            continue
        tail = ASSIGN_TAIL_RE.match(text, kw.end())
        if not tail:
            continue
        start, end = tail.start(1), tail.end(1)
        if start < pos:
            continue
        out.append(text[pos:start])
        out.append("«redacted»")
        pos = end
    out.append(text[pos:])
    text = "".join(out)
    text = FLAG_SECRET_RE.sub(lambda m: f"{m.group(1)} {m.group(2)}«redacted»", text)
    text = USER_PW_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}:«redacted»", text)
    text = URL_CRED_RE.sub(lambda m: m.group(1) + ":«redacted»@", text)
    return BLOB_RE.sub("«redacted»", text)


ANSWER_PAIR_RE = re.compile(r'"[^"]*"\s*=\s*"([^"]*)"')


def parse_question_answers(raw: str) -> list[str]:
    """Pull the chosen labels out of an AskUserQuestion tool result."""
    pairs = ANSWER_PAIR_RE.findall(raw or "")
    if pairs:
        return [one_line(p, 160) for p in pairs]
    cleaned = re.sub(r"^\s*The user answered:\s*", "", raw or "").strip()
    return [one_line(cleaned, 200)] if cleaned else []


def blocks_of(message: object) -> list:
    """Return content blocks as a list of dicts, whatever shape we were given."""
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = message
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def text_of(message: object) -> str:
    # b["text"] may be present-but-null, or a non-string; never let join() raise.
    return "\n".join(
        str(b.get("text") or "") for b in blocks_of(message) if b.get("type") == "text"
    ).strip()


def stringify(value: object, limit: int = 400) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return one_line(value, limit)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        return one_line(" ".join(parts), limit)
    if isinstance(value, dict):
        for key in ("text", "content", "stdout", "stderr", "error", "message", "result"):
            if value.get(key):
                return stringify(value[key], limit)
        return one_line(json.dumps(value, ensure_ascii=False), limit)
    return one_line(str(value), limit)


def is_error_result(block: dict, entry: dict) -> bool:
    for key in ("is_error", "isError"):
        if block.get(key) is True:
            return True
    tur = entry.get("toolUseResult") or entry.get("tool_use_result")
    if isinstance(tur, dict):
        for key in ("is_error", "isError", "error"):
            if tur.get(key):
                return True
        if tur.get("interrupted") is True:
            return True
    text = stringify(block.get("content"), 200)
    return text.startswith("Error:") or text.startswith("<tool_use_error>")


def git(cwd: str, *args: str, timeout: int = 6) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def git_time(ts: str) -> str:
    """Transcript timestamps look like 2026-03-10T11:00:00.000Z. git silently
    treats that as unparseable and returns an empty log, so normalise to a form
    git's date parser accepts, with the timezone made explicit."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S %z")
    except Exception:
        return ""


def repo_identity(cwd: str) -> dict:
    """Resolve the *canonical* repo for a directory, even inside a git worktree.

    `git rev-parse --show-toplevel` returns the worktree root, so a worktree at
    ../feat-x reports repo "feat-x" and its notes stop merging with the main
    repo's. `--git-common-dir` always points at the shared .git of the real
    repository, so derive the name from that and keep the worktree as a separate
    label.
    """
    info = {
        "repo": Path(cwd).name if cwd else "unknown",
        "worktree": "",
        "work_root": cwd,  # where to run diff/log — the checkout, not the repo
        "guessed": True,
    }
    if not cwd:
        return info

    probe = cwd
    if not Path(cwd).is_dir():
        # Backfilled session whose worktree has since been removed: walk up for
        # a directory that still exists and is still a repo.
        for parent in Path(cwd).parents:
            if parent.is_dir() and git(str(parent), "rev-parse", "--is-inside-work-tree") == "true":
                probe = str(parent)
                break
        else:
            return info

    toplevel = git(probe, "rev-parse", "--show-toplevel")
    common = git(probe, "rev-parse", "--git-common-dir")
    if not common:
        return info

    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = Path(toplevel or probe) / common
    try:
        common_path = common_path.resolve()
    except Exception:
        pass

    if common_path.name == ".git":
        repo_root = common_path.parent
        repo_name = repo_root.name
    elif common_path.name.endswith(".git"):  # bare repo: myrepo.git
        repo_root = common_path
        repo_name = common_path.name[:-4]
    else:
        repo_root = common_path
        repo_name = common_path.name

    info["repo"] = repo_name or info["repo"]
    info["guessed"] = False
    info["work_root"] = toplevel or probe
    if toplevel and Path(toplevel).resolve() != repo_root:
        info["worktree"] = Path(toplevel).name
    return info


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9가-힣._-]+", "-", (text or "").strip()).strip("-")
    return (slug or "session")[:60]


# --------------------------------------------------------------------------- #
# transcript parsing
# --------------------------------------------------------------------------- #
def parse_transcript(path: Path, cfg: dict) -> dict:
    facts = {
        "prompts": [],
        "plans": [],
        "questions": [],
        "files": {},
        "commands": [],
        "errors": [],
        "subagents": [],
        "research": [],
        "assistant_turns": 0,
        "sidechain_entries": 0,
        "first_ts": None,
        "last_ts": None,
        "branch": None,
        "cwd": None,
        "models": set(),
        "lines_read": 0,
        "parse_errors": 0,
    }

    tool_names: dict[str, str] = {}
    tool_inputs: dict[str, dict] = {}
    seen_cmds: set[str] = set()

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            facts["lines_read"] += 1
            try:
                entry = json.loads(raw)
            except Exception:
                facts["parse_errors"] += 1
                continue
            if not isinstance(entry, dict):
                continue
            # One malformed entry must cost one entry, not the whole digest —
            # that is the plugin's core promise when the transcript format drifts.
            try:
                _consume_entry(entry, facts, cfg, tool_names, tool_inputs, seen_cmds)
            except Exception:
                facts["parse_errors"] += 1

    return facts


def _consume_entry(
    entry: dict,
    facts: dict,
    cfg: dict,
    tool_names: dict,
    tool_inputs: dict,
    seen_cmds: set,
) -> None:
    ts = entry.get("timestamp")
    if isinstance(ts, str):
        if facts["first_ts"] is None:
            facts["first_ts"] = ts
        facts["last_ts"] = ts
    if entry.get("gitBranch"):
        facts["branch"] = entry["gitBranch"]
    if entry.get("cwd"):
        facts["cwd"] = entry["cwd"]

    if entry.get("isSidechain") is True:
        facts["sidechain_entries"] += 1
        return

    raw_msg = entry.get("message")
    etype = entry.get("type") or (raw_msg.get("role") if isinstance(raw_msg, dict) else None)
    message = raw_msg if isinstance(raw_msg, dict) else entry

    if etype == "assistant":
        facts["assistant_turns"] += 1
        model = message.get("model")
        if isinstance(model, str) and model and not model.startswith("<"):
            facts["models"].add(model)  # "<synthetic>" and non-str shapes skipped
        for block in blocks_of(message):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name") or ""
            tid = block.get("id") or ""
            tinput = block.get("input") if isinstance(block.get("input"), dict) else {}
            if tid:
                tool_names[tid] = name
                tool_inputs[tid] = tinput

            if name == "ExitPlanMode":
                raw_plan = tinput.get("plan")
                plan = (
                    truncate(raw_plan, 1800)
                    if isinstance(raw_plan, str)
                    else stringify(raw_plan, 1800)
                )
                if plan:
                    facts["plans"].append(redact(plan, cfg["redact"]))
            elif name == "AskUserQuestion":
                qs = tinput.get("questions")
                if isinstance(qs, list):
                    for q in qs:
                        if not isinstance(q, dict):
                            continue
                        opts = [
                            one_line(str(o.get("label", "")), 60)
                            for o in (q.get("options") or [])
                            if isinstance(o, dict)
                        ]
                        facts["questions"].append(
                            {
                                "tid": tid,
                                "q": one_line(str(q.get("question", "")), 240),
                                "options": opts,
                                "answer": "",
                            }
                        )
            elif name in FILE_TOOLS:
                fp = tinput.get("file_path") or tinput.get("path") or ""
                if fp:
                    facts["files"][fp] = facts["files"].get(fp, 0) + 1
            elif name == "Bash":
                cmd = one_line(str(tinput.get("command", "")), 220)
                if cmd and INTERESTING_CMD.search(cmd):
                    cmd = redact(cmd, cfg["redact"])
                    if cmd not in seen_cmds:
                        seen_cmds.add(cmd)
                        facts["commands"].append(cmd)
            elif name in ("Task", "Agent"):
                facts["subagents"].append(
                    redact(
                        one_line(
                            f"{tinput.get('subagent_type') or 'agent'}: "
                            f"{tinput.get('description') or stringify(tinput.get('prompt'), 160)}",
                            200,
                        ),
                        cfg["redact"],
                    )
                )
            elif name in ("WebSearch", "WebFetch", "mcp__workspace__web_fetch"):
                facts["research"].append(
                    redact(
                        one_line(str(tinput.get("query") or tinput.get("url") or ""), 160),
                        cfg["redact"],
                    )
                )
        return

    # user-role entries are either real prompts or tool results
    blocks = blocks_of(message)
    results = [b for b in blocks if b.get("type") == "tool_result"]
    if results:
        for block in results:
            tid = block.get("tool_use_id") or ""
            name = tool_names.get(tid, "?")
            if name == "AskUserQuestion":
                answers = [
                    redact(a, cfg["redact"])
                    for a in parse_question_answers(stringify(block.get("content"), 900))
                ]
                targets = [q for q in facts["questions"] if q.get("tid") == tid]
                if not targets:
                    targets = [q for q in facts["questions"] if not q["answer"]]
                for q, ans in zip(targets, answers):
                    q["answer"] = ans
                # More answers than questions parsed: keep the extras visible.
                for extra in answers[len(targets) :]:
                    facts["questions"].append(
                        {"tid": tid, "q": "(질문 미기록)", "options": [], "answer": extra}
                    )
                continue
            if is_error_result(block, entry) and name not in NOISE_TOOLS:
                detail = stringify(block.get("content"), 320)
                target = (
                    tool_inputs.get(tid, {}).get("command")
                    or tool_inputs.get(tid, {}).get("file_path")
                    or ""
                )
                facts["errors"].append(
                    {
                        "tool": name,
                        "target": one_line(str(target), 120),
                        "detail": redact(detail, cfg["redact"]),
                    }
                )
        return

    if entry.get("isMeta") is True or entry.get("isCompactSummary") is True:
        return
    prompt = text_of(message)
    if not prompt:
        return
    if prompt.startswith("<") and re.match(
        r"<(command-name|command-message|local-command|bash-input|system-reminder)", prompt
    ):
        return
    if prompt.startswith("[Request interrupted"):
        return
    facts["prompts"].append(
        redact(truncate(prompt, cfg["max_prompt_chars"]), cfg["redact"])
    )


# --------------------------------------------------------------------------- #
# digest rendering
# --------------------------------------------------------------------------- #
def build_digest(facts: dict, meta: dict, cfg: dict) -> str:
    cwd = meta["cwd"]
    ident = meta.get("ident") or repo_identity(cwd)
    repo = ident["repo"]
    worktree = ident["worktree"]
    work_root = ident["work_root"]
    branch = facts["branch"] or (git(work_root, "rev-parse", "--abbrev-ref", "HEAD") if work_root else "")
    # Paths are trimmed against the checkout the session actually ran in.
    repo_root = git(work_root, "rev-parse", "--show-toplevel") if work_root else ""

    commits, dirty = "", ""
    if repo_root:
        # Bound the log to the session window on both sides — a backfilled
        # March session must not list every commit made since March.
        args = ["log", "--pretty=%h %s", "-n", "20"]
        since, until = git_time(facts["first_ts"] or ""), git_time(facts["last_ts"] or "")
        if since:
            args.append(f"--since={since}")
        if until:
            args.append(f"--until={until}")
        # A window needs both ends: --since alone would drag in every commit
        # made after an old backfilled session.
        commits = git(repo_root, *args) if (since and until) or not facts["first_ts"] else ""
        # `git diff` shows the checkout as it is *now*; that only describes the
        # session if the session just ended. Skip it for old (backfilled) ones.
        recent = True
        try:
            if facts["last_ts"]:
                ended = datetime.fromisoformat(facts["last_ts"].replace("Z", "+00:00"))
                now = datetime.now(ended.tzinfo) if ended.tzinfo else datetime.now()
                recent = (now - ended).total_seconds() < 6 * 3600
        except Exception:
            pass
        if recent:
            dirty = git(repo_root, "diff", "--shortstat", "HEAD")

    duration = ""
    try:
        if facts["first_ts"] and facts["last_ts"]:
            a = datetime.fromisoformat(facts["first_ts"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(facts["last_ts"].replace("Z", "+00:00"))
            mins = max(0, int((b - a).total_seconds() // 60))
            duration = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"
    except Exception:
        pass

    files = sorted(facts["files"].items(), key=lambda kv: -kv[1])[: cfg["max_files"]]
    started = (facts["first_ts"] or datetime.now(timezone.utc).isoformat())[:19]

    out: list[str] = []
    add = out.append

    add("---")
    add('type: dev-journal-raw')
    add("status: unprocessed")
    add(f'session_id: "{meta["session_id"]}"')
    add(f'repo: "{repo}"')
    add(f'worktree: "{worktree}"')
    add(f'repo_name_guessed: {"true" if ident["guessed"] else "false"}')
    add(f'branch: "{branch}"')
    add(f'cwd: "{cwd}"')
    add(f'started: "{started}"')
    add(f'ended: "{(facts["last_ts"] or "")[:19]}"')
    add(f'duration: "{duration}"')
    add(f'end_reason: "{meta.get("reason", "")}"')
    add(f"turns: {facts['assistant_turns']}")
    add(f"files_touched: {len(facts['files'])}")
    add(f"errors_hit: {len(facts['errors'])}")
    add(f'models: "{", ".join(sorted(facts["models"]))}"')
    add("---")
    add("")
    heading = f"# Raw session digest — {repo}"
    if worktree:
        heading += f" (worktree `{worktree}`)"
    add(f"{heading} — {started[:10]}")
    add("")
    if worktree:
        add(
            f"> Ran in git worktree `{worktree}` of **{repo}**. "
            f"Knowledge belongs to `{repo}`, not to the worktree."
        )
        add("")
    if ident["guessed"]:
        add(
            f"> ⚠️ `{cwd}` is no longer a git repo (worktree removed?), so `repo: {repo}` "
            "is inferred from the directory name and may be the worktree's name rather "
            "than the repository's. Verify before filing this under a project note."
        )
        add("")
    add(
        "> Machine-extracted facts, no interpretation. "
        "Synthesised by the `dev-journal` skill; do not hand-edit."
    )
    add("")

    lines_read = facts.get("lines_read", 0)
    if facts.get("parse_errors") and lines_read and facts["parse_errors"] > lines_read * 0.1:
        add(
            f"> ⚠️ {facts['parse_errors']}/{lines_read} transcript lines were unparseable — "
            "the Claude Code transcript format may have changed. "
            "Run `dev-journal-setup` diagnostics."
        )
        add("")
    if facts["assistant_turns"] == 0 and not facts["prompts"]:
        add("> ⚠️ Nothing recognisable was extracted from this transcript.")
        add("")

    add("## What I asked for (verbatim prompts)")
    add("")
    prompts = facts["prompts"][: cfg["max_prompts"]]
    if prompts:
        for i, p in enumerate(prompts, 1):
            add(f"{i}. {p}")
        if len(facts["prompts"]) > len(prompts):
            add(f"…and {len(facts['prompts']) - len(prompts)} more prompts.")
    else:
        add("_none captured_")
    add("")

    if facts["questions"]:
        add("## Explicit choices I made (AskUserQuestion)")
        add("")
        for q in facts["questions"]:
            add(f"- **Q:** {q['q']}")
            if q["options"]:
                add(f"  - offered: {', '.join(q['options'])}")
            add(f"  - **chose:** {q['answer'] or '(no answer recorded)'}")
        add("")

    if facts["plans"]:
        add("## Plans approved (ExitPlanMode)")
        add("")
        for plan in facts["plans"][:5]:
            add("```")
            add(plan)
            add("```")
        add("")

    add("## Files touched")
    add("")
    if files:
        for fp, n in files:
            rel = fp
            if repo_root and fp.startswith(repo_root):
                rel = fp[len(repo_root) :].lstrip("/")
            add(f"- `{rel}` ({n} edit{'s' if n > 1 else ''})")
        if len(facts["files"]) > len(files):
            add(f"- …and {len(facts['files']) - len(files)} more files")
    else:
        add("_no file edits_")
    add("")

    if facts["commands"]:
        add("## Notable commands run")
        add("")
        for cmd in facts["commands"][: cfg["max_commands"]]:
            add(f"- `{cmd}`")
        add("")

    if facts["errors"]:
        add("## Errors / failures encountered")
        add("")
        for err in facts["errors"][: cfg["max_errors"]]:
            target = f" on `{err['target']}`" if err["target"] else ""
            add(f"- **{err['tool']}**{target}: {err['detail']}")
        add("")

    if commits:
        add("## Commits during session")
        add("")
        for line in commits.splitlines()[:20]:
            add(f"- {line}")
        add("")

    if dirty:
        add(f"## Uncommitted at session end\n\n- {dirty}\n")

    if facts["subagents"]:
        add("## Subagents dispatched")
        add("")
        for s in facts["subagents"][:15]:
            add(f"- {s}")
        add("")

    if facts["research"]:
        add("## Looked up externally")
        add("")
        for r in dict.fromkeys(facts["research"]):
            add(f"- {r}")
        add("")

    text = "\n".join(out)
    if len(text) > cfg["max_digest_chars"]:
        text = text[: cfg["max_digest_chars"]] + "\n\n_[digest truncated]_\n"
    return text


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def resolve_output_dir(cfg: dict) -> tuple[Path, bool]:
    vault = (cfg.get("vault_path") or "").strip()
    if vault:
        vault_path = Path(os.path.expanduser(vault))
        if vault_path.is_dir():
            return vault_path / cfg["base_folder"] / "_raw", True
        log(f"vault_path '{vault}' is not a directory; staging locally")
    return STATE_DIR / "staged", False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", help="file containing the hook JSON payload")
    ap.add_argument("--transcript", help="parse this transcript directly (for testing)")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--session-id", default="manual")
    ap.add_argument("--force", action="store_true", help="ignore min-signal thresholds")
    ap.add_argument("--stdout", action="store_true", help="print digest instead of writing")
    args = ap.parse_args()

    cfg = load_config()
    payload: dict = {}
    if args.payload:
        try:
            with open(args.payload, "r", encoding="utf-8") as fh:
                payload = json.load(fh) or {}
        except Exception as exc:
            log(f"unreadable payload {args.payload}: {exc}")
        finally:
            try:
                os.remove(args.payload)
            except OSError:
                pass

    transcript = args.transcript or payload.get("transcript_path") or ""
    cwd = payload.get("cwd") or args.cwd or ""
    session_id = payload.get("session_id") or args.session_id or "unknown"
    # "manual" would make every ad-hoc run of the same repo/day collide on one
    # filename; the transcript stem is the real session id.
    if session_id == "manual" and transcript:
        session_id = Path(transcript).stem
    reason = payload.get("reason") or payload.get("hook_event_name") or ""

    if not transcript or not Path(transcript).is_file():
        log(f"no transcript at '{transcript}'; nothing to do")
        return 0

    for excluded in cfg.get("exclude_paths") or []:
        if excluded and excluded in cwd:
            log(f"cwd matches exclude_paths entry '{excluded}'; skipping")
            return 0

    facts = parse_transcript(Path(transcript), cfg)

    # Transcripts without timestamps (older formats) must not be dated "today" —
    # that would file a March session under an August backfill run. The file's
    # mtime is the session's last activity; use it as the anchor.
    if not facts["first_ts"]:
        try:
            mtime = datetime.fromtimestamp(Path(transcript).stat().st_mtime, tz=timezone.utc)
            facts["first_ts"] = facts["first_ts"] or mtime.isoformat()
            facts["last_ts"] = facts["last_ts"] or mtime.isoformat()
        except OSError:
            pass

    enough = args.force or (
        len(facts["prompts"]) >= cfg["min_user_prompts"]
        # "min N files" means N suffices; floor of 1 so pure-Q&A sessions with
        # zero edits don't pass on this branch.
        or len(facts["files"]) >= max(int(cfg["min_files_touched"] or 0), 1)
    )
    if not enough:
        log(
            f"session {session_id[:8]} below signal threshold "
            f"({len(facts['prompts'])} prompts, {len(facts['files'])} files); skipping"
        )
        return 0

    # The payload's cwd is wherever the session *ended*; the transcript records
    # where it ran. Prefer the transcript so PreCompact and SessionEnd captures
    # of one session agree on the filename even if the user cd'd away.
    cwd = facts["cwd"] or cwd

    # Privacy gate must be re-run against the resolved cwd: the earlier check
    # only saw the payload's cwd, and a session that ran inside an excluded
    # repo but ended elsewhere would otherwise slip through.
    for excluded in cfg.get("exclude_paths") or []:
        if excluded and excluded in cwd:
            log(f"resolved cwd matches exclude_paths entry '{excluded}'; skipping")
            return 0

    ident = repo_identity(cwd)
    digest = build_digest(
        facts,
        {"cwd": cwd, "session_id": session_id, "reason": reason, "ident": ident},
        cfg,
    )

    if args.stdout:
        sys.stdout.write(digest)
        return 0

    out_dir, in_vault = resolve_output_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    day = (facts["first_ts"] or datetime.now(timezone.utc).isoformat())[:10]
    # Filenames group by canonical repo so worktree sessions sort next to the
    # main repo's; the worktree stays visible as a suffix.
    repo_hint = slugify(ident["repo"] or "session")
    if ident["worktree"]:
        repo_hint += f"~{slugify(ident['worktree'])[:20]}"
    out_file = out_dir / f"{day}--{repo_hint}--{slugify(session_id)[-8:]}.md"

    # PreCompact and SessionEnd can both fire for one session, possibly with two
    # capture processes alive at once. Two rules: never trade a richer digest for
    # a thinner one, and never clobber a digest already synthesised. Checks are
    # anchored to the frontmatter (first ~600 chars) so a captured prompt that
    # merely *contains* "status: processed" can't fake them, and the write is
    # tmp + os.replace so a concurrent reader never sees a torn file.
    def should_keep_existing() -> bool:
        try:
            head = out_file.read_text(encoding="utf-8", errors="replace")[:600]
        except OSError:
            return False
        if re.search(r"^status: processed$", head, re.M):
            log(f"{out_file.name} already synthesised; leaving it alone")
            return True
        prev = re.search(r"^turns: (\d+)$", head, re.M)
        if prev and int(prev.group(1)) > facts["assistant_turns"]:
            log(
                f"{out_file.name} has a richer digest "
                f"({prev.group(1)} turns > {facts['assistant_turns']}); keeping it"
            )
            return True
        return False

    if out_file.exists() and should_keep_existing():
        return 0

    tmp_file = out_file.with_name(f".{out_file.name}.{os.getpid()}.tmp")
    tmp_file.write_text(digest, encoding="utf-8")
    # Re-check at the last moment: a concurrent capture may have landed a richer
    # digest while we were parsing.
    if out_file.exists() and should_keep_existing():
        tmp_file.unlink(missing_ok=True)
        return 0
    os.replace(tmp_file, out_file)

    where = "vault" if in_vault else "local staging (vault_path not set)"
    log(f"wrote {out_file} ({len(digest)} chars) to {where}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never let a hook-spawned process die loudly
        log(f"unexpected failure: {exc!r}")
        sys.exit(0)
