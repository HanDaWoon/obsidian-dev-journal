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
import socket
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
    # Machine label written into every digest. Empty -> the hostname's first
    # label. Only recordable at capture time, so it cannot be backfilled later;
    # see host_name().
    "host_name": "",
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
    "max_recaps": 12,
    "max_recap_chars": 400,
    "max_digest_chars": 14000,
    # Extra regex OR'd into INTERESTING_CMD, for domain tooling an allowlist
    # cannot know in advance (e.g. "\\b(daml|canton|sui|vast|orca|tsh)\\b").
    "extra_commands": "",
    # Directories/repos to never journal (substring match on cwd).
    "exclude_paths": [],
    # Redact obvious secrets from captured commands.
    "redact": True,
}

# Bash commands worth remembering. Everything else (ls, cat, cd...) is noise.
#
# The remote/ops verbs are here because an allowlist that stops at build tools
# throws away whole categories of work: on the local corpus 6027 of 8706 bash
# calls were dropped, led by ssh (1242), python/python3 (491) and curl (86) —
# and 29 of 133 digests ended up with an empty "Notable commands" section even
# though one of them ran 1468 commands. Fleet work *is* ssh.
#
# An allowlist still cannot know a user's domain tooling (daml, canton, sui,
# vast, orca, tsh...), so `extra_commands` in the config extends this — see
# command_matcher().
INTERESTING_CMD = re.compile(
    r"\b(git|gh|npm|pnpm|yarn|bun|npx|make|just|task|docker|docker-compose|kubectl|helm|"
    r"terraform|tofu|ansible|ansible-playbook|pytest|jest|vitest|mocha|go|cargo|mvn|gradle|rake|bundle|"
    r"poetry|uv|pip|pipx|python|python3|ruff|mypy|eslint|prettier|tsc|alembic|prisma|psql|mysql|redis-cli|"
    r"aws|gcloud|az|flyctl|vercel|wrangler|supabase|dbt|serverless|sst|"
    r"ssh|scp|rsync|curl|wget|systemctl|journalctl|launchctl|crontab)\b"
)

NOISE_TOOLS = {"TodoWrite", "Read", "Grep", "Glob", "LS", "NotebookRead", "ToolSearch"}
FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "StrReplace"}

# A failing shell command usually comes back as an *ordinary* tool_result: the
# harness sets is_error for tool-level failures, so `pytest` reporting "5 failed"
# or a python traceback read as success. Measured over 218 local transcripts:
# 345 errors were captured while 243 shell failures were not — and those are
# exactly the error → root cause → fix chains the Learnings notes come from.
# 22 digests with 50+ turns claimed `errors_hit: 0`.
#
# Deliberately narrow. Broad matching floods the section with noise: every one
# of the 673 results with a non-empty `stderr` in that corpus was the harness's
# own "Shell cwd was reset", and a bare /error:/ matches `ls` probes and grep
# output. Anchors are avoided because the text is whitespace-collapsed first.
SHELL_FAILURE_RE = re.compile(
    r"Traceback \(most recent call last\):"
    r"|command not found"
    r"|\bfatal: "
    r"|error TS\d+:"
    r"|={3,} FAILURES"
    # "5 failed, 263 passed" is a failure; "ok: 277 failed: 0" is a summary
    # line where the count belongs to the word before it.
    r"|\b(?!0\b)\d+ failed\b(?!:)"
    r"|\bfailed=[1-9]"
    r"|\b(?:AssertionError|ModuleNotFoundError|ImportError|SyntaxError|OperationalError)\b"
    r"|\bconnection refused\b"
    r"|\bno space left on device\b",
    re.I,
)

# Entry types that carry text but are not the user speaking.
#   system          — harness recaps ("Goal was X. Next: Y."). High-signal, but
#                     assistant-generated: they belong in their own section, not
#                     quoted under "what I asked for".
#   queue-operation — input queued while Claude was busy. The transcript records
#                     it here AND (usually) again as a real `user` entry, so it
#                     is a *shadow*: keep it only when nothing else carries the
#                     same text. See dedupe_prompts().
SYSTEM_ENTRY_TYPES = {"system"}
SHADOW_ENTRY_TYPES = {"queue-operation"}

# Text the harness injects into the user channel. Same class as the
# <command-name>/<system-reminder> blocks filtered below: auto-generated, zero
# user intent, and it crowds real prompts out of the max_prompts budget.
AUTO_PROMPT_RE = re.compile(
    r"^(?:<task-notification>|You have \d+ orchestration messages?\b)"
)

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
# The lookbehind makes the dash start a real flag. Without it the "-keygen" of
# `ssh-keygen -R host` matched as a flag and the *next word* was redacted —
# observed in the vault, where it ate the host being removed.
#
# The quoted branch comes first and consumes the closing quote, because a value
# is allowed to contain spaces: `--secret-key 'abc def'` used to redact only up
# to the space and leave `'«redacted» def'` in the digest. The unquoted branch
# still tolerates a stray opening quote, so an unterminated `--key "abc` is
# masked rather than skipped.
FLAG_SECRET_RE = re.compile(
    r"(?i)((?<![A-Za-z0-9])--?[A-Za-z0-9-]*(?:key|token|secret|password|passwd|credential)[A-Za-z0-9-]*|"
    r"\bset\s+[A-Za-z0-9_]*(?:key|token|secret|password)[A-Za-z0-9_]*)"
    r"\s+(?:([\"'])([^\"']{1,200})\2|([\"']?)([^\s\"']{1,200}))"
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


def command_matcher(cfg: dict) -> "re.Pattern":
    """INTERESTING_CMD, widened by the user's `extra_commands` regex.

    A bad regex in the config must not take the whole capture down with it —
    the digest degrades to the built-in allowlist and says so in the log.
    """
    extra = (cfg.get("extra_commands") or "").strip()
    if not extra:
        return INTERESTING_CMD
    try:
        return re.compile(INTERESTING_CMD.pattern + "|" + extra)
    except re.error as exc:
        log(f"extra_commands is not a valid regex ({exc}); using the built-in list")
        return INTERESTING_CMD


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
        # A keyword that is merely the *prefix* of a longer lowercase word is a
        # different word: author, authorized, authentication, secretary,
        # tokenizer. Redacting those destroyed real content — the vault has
        # `com.digitalasset.auth.TokenService:«redacted»` and
        # `"authorizedStoreSnapshot":"«redacted»`, where the masked part was an
        # error message and a diagnostic, not a credential.
        # A trailing "s" is still the same word (tokens, credentials), and
        # "authorization" is a real header name, so both stay in.
        ident_end = kw.end()
        while ident_end < len(text) and (text[ident_end].isalnum() or text[ident_end] in "_"):
            ident_end += 1
        ident_start = kw.start()
        while ident_start > 0 and (text[ident_start - 1].isalnum() or text[ident_start - 1] in "_"):
            ident_start -= 1
        run_end = kw.end()
        while run_end < len(text) and "a" <= text[run_end] <= "z":
            run_end += 1
        run = text[kw.end() : run_end]
        if run and run != "s" and text[ident_start:ident_end].lower() != "authorization":
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
    # Groups: 1 flag, 2/3 quote+value (quoted branch), 4/5 stray quote+value
    # (unquoted branch). The quoted branch consumed its closing quote, so put it
    # back; the unquoted branch never had one to consume.
    text = FLAG_SECRET_RE.sub(
        lambda m: (
            f"{m.group(1)} {m.group(2)}«redacted»{m.group(2)}"
            if m.group(2)
            else f"{m.group(1)} {m.group(4) or ''}«redacted»"
        ),
        text,
    )
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


def dedupe_prompts(entries: list[tuple[str, str]]) -> list[str]:
    """Collapse the transcript's double-recording of queued input.

    `entries` is chronological (source, text) where source is "user" or
    "shadow". Rules, in order of importance:

      1. A shadow whose text also appears as a real user prompt is dropped —
         it is the same input recorded twice.
      2. Repeated shadows of the same text collapse to one. The transcript
         writes a queued message when it is queued *and* when it is delivered.
      3. Real user prompts are never deduped. Saying "다시" twice is two turns,
         and collapsing them would misrepresent the session.

    Measured on 148 local transcripts: 2152 extracted prompts -> 977 of them
    were shadows or system recaps, i.e. the prompt budget was ~half noise.
    """
    user_texts = {text for source, text in entries if source == "user"}
    out: list[str] = []
    seen_shadows: set[str] = set()
    for source, text in entries:
        if source == "shadow":
            if text in user_texts or text in seen_shadows:
                continue
            seen_shadows.add(text)
        out.append(text)
    return out


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
        # Why the name is a guess. "worktree removed?" was printed for every
        # case, including plain directories that were never repos, which sent
        # the reader hunting for a worktree that never existed.
        "reason": "no working directory recorded",
    }
    if not cwd:
        return info

    info["reason"] = "the directory no longer exists (worktree removed?)"
    probe = cwd
    if Path(cwd).is_dir():
        info["reason"] = "it is not a git repository"
    if not Path(cwd).is_dir():
        # Backfilled session whose worktree has since been removed: walk up for
        # a directory that still exists and is still a repo. Bounded, because an
        # unbounded climb reaches ~ — and a home directory under dotfiles
        # management is a git repo, so every deleted checkout would confidently
        # report the user's home as its repo.
        for parent in list(Path(cwd).parents)[:4]:
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
        # git resolves --git-common-dir against the directory it ran in (the -C
        # target), NOT against the worktree root. Anchoring it to toplevel walks
        # the ".." out of the repository: from ~/Work/repo/tests git answers
        # "../.git", which against toplevel becomes ~/Work/.git — repo "Work",
        # worktree "repo", guessed=False, so nothing warns. Measured on the
        # local corpus: 15 of 218 sessions started in a subdirectory and 11 of
        # 133 digests carried a repo name that way. Anchor to probe.
        common_path = Path(probe) / common
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
    info["reason"] = ""
    info["work_root"] = toplevel or probe
    if toplevel and Path(toplevel).resolve() != repo_root:
        info["worktree"] = Path(toplevel).name
    return info


def refine_identity(cwd: str, counts: dict) -> dict:
    """repo_identity, allowed to consult every directory the session visited.

    Two things the most-visited directory alone gets wrong, both seen in the
    real vault — and both only when it is already a *guess*, so a cleanly
    resolved repo is never second-guessed:

    1. It can be a plain parent folder holding repos. A session that sat in
       `~/Work/GPU-cluster` (not a repo) but did its work in
       `.../ansible_boundless` (a repo) must be filed under the repo. So among
       the directories the session actually visited, the most-visited one that
       git can resolve wins.
    2. Failing that, a leaf is a poor project name: `<project>/scripts` was
       filed as repo "scripts", `<project>/outputs` as "outputs". Back off to
       the shallowest visited directory containing the chosen one.

    Candidates are always restricted to directories the session was really in,
    so this cannot wander off into an unrelated repo the way an unbounded
    parent walk does.
    """
    ident = repo_identity(cwd)
    if not ident["guessed"] or not cwd:
        return ident

    for cand in sorted(counts, key=lambda c: -counts[c]):
        if cand == cwd:
            continue
        alt = repo_identity(cand)
        if not alt["guessed"]:
            return alt

    ancestors = sorted(
        (c for c in counts if c != cwd and cwd.startswith(c.rstrip("/") + "/")), key=len
    )
    if ancestors:
        alt = repo_identity(ancestors[0])
        alt["reason"] = alt["reason"] or ident["reason"]
        return alt
    return ident


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9가-힣._-]+", "-", (text or "").strip()).strip("-")
    return (slug or "session")[:60]


def host_name(cfg: dict) -> str:
    """Which machine this session ran on.

    Only matters later, when two machines' vaults get merged into one: the
    digests are keyed by session_id and carry no other clue about their origin
    (`cwd` differs per machine but says nothing reliable about *which* one).
    This is capture-time-only information — a digest written before this field
    existed can never learn its host, so the field is worth writing early.

    `socket.gethostname()` returns `mbp.local` / `mbp.lan` / a DHCP-assigned
    name depending on the network, so only the first label is kept. A user who
    wants a stable friendly name sets `host_name` in the config.
    """
    name = (cfg.get("host_name") or "").strip()
    if not name:
        try:
            name = (socket.gethostname() or "").split(".")[0]
        except OSError:  # no hostname is not a reason to lose the digest
            name = ""
    # Sanitised here rather than through slugify(), whose "session" fallback for
    # an empty result would turn a junk hostname into a plausible-looking host
    # on every digest. Inventing a machine is worse than admitting to none.
    return re.sub(r"[^A-Za-z0-9가-힣._-]+", "-", name).strip("-.")[:40]


# --------------------------------------------------------------------------- #
# transcript parsing
# --------------------------------------------------------------------------- #
def parse_transcript(path: Path, cfg: dict) -> dict:
    facts = {
        "prompts": [],
        # Chronological (source, text) before dedupe; see dedupe_prompts().
        "prompt_entries": [],
        "recaps": [],
        "plans": [],
        "questions": [],
        "files": {},
        "commands": [],
        "errors": [],
        "subagents": [],
        "research": [],
        # Failures recognised from a command's own output rather than from an
        # is_error flag; capped separately so a chatty log cannot crowd out the
        # real tool errors.
        "inferred_errors": 0,
        "assistant_turns": 0,
        "sidechain_entries": 0,
        "first_ts": None,
        "last_ts": None,
        "branch": None,
        "cwd": None,
        # Every cwd the session reported, in first-seen order, with counts.
        # See resolve_cwd().
        "cwd_counts": {},
        "models": set(),
        "lines_read": 0,
        "parse_errors": 0,
    }

    tool_names: dict[str, str] = {}
    tool_inputs: dict[str, dict] = {}
    seen_cmds: set[str] = set()
    cmd_re = command_matcher(cfg)

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
                _consume_entry(entry, facts, cfg, tool_names, tool_inputs, seen_cmds, cmd_re)
            except Exception:
                facts["parse_errors"] += 1

    facts["prompts"] = dedupe_prompts(facts["prompt_entries"])
    facts["cwd"] = resolve_cwd(facts["cwd_counts"])
    return facts


def resolve_cwd(counts: dict) -> "str | None":
    """Which directory *was* this session?

    Not the last one. `cwd` was previously overwritten on every entry, so a
    session that ended with one `cd` into `tests/` was filed under whatever repo
    that resolved to — while the note's date came from the *first* timestamp,
    and journal_util's own transcript_cwd() read the *first* cwd. Three parts of
    the plugin disagreed about the same session.

    16 of 218 local transcripts have first != last. The directory the session
    spent most of its entries in is the honest answer; ties go to the earliest,
    which keeps single-cwd sessions (the overwhelming majority) unchanged.
    """
    if not counts:
        return None
    order = {cwd: i for i, cwd in enumerate(counts)}
    return max(counts, key=lambda c: (counts[c], -order[c]))


def _consume_entry(
    entry: dict,
    facts: dict,
    cfg: dict,
    tool_names: dict,
    tool_inputs: dict,
    seen_cmds: set,
    cmd_re: "re.Pattern | None" = None,
) -> None:
    cmd_re = cmd_re or INTERESTING_CMD
    ts = entry.get("timestamp")
    if isinstance(ts, str):
        if facts["first_ts"] is None:
            facts["first_ts"] = ts
        facts["last_ts"] = ts
    if entry.get("gitBranch"):
        facts["branch"] = entry["gitBranch"]
    if entry.get("cwd"):
        facts["cwd_counts"][entry["cwd"]] = facts["cwd_counts"].get(entry["cwd"], 0) + 1

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
                if cmd and cmd_re.search(cmd):
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
            failed = is_error_result(block, entry) and name not in NOISE_TOOLS
            detail = ""
            inferred = False
            if not failed and name == "Bash" and facts["inferred_errors"] < cfg["max_errors"]:
                body = stringify(block.get("content"), 6000)
                hit = SHELL_FAILURE_RE.search(body)
                if hit:
                    failed = inferred = True
                    facts["inferred_errors"] += 1
                    # The signature is usually at the *end* of the output (a
                    # pytest summary, the last frame of a traceback), so quote
                    # around the match instead of the first 320 chars.
                    detail = one_line(body[max(0, hit.start() - 80) : hit.start() + 240], 320)
            if failed:
                detail = detail or stringify(block.get("content"), 320)
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
                        # Rendered differently: the harness never called this a
                        # failure, the output only looks like one. The reader
                        # has to be able to discount it.
                        "inferred": inferred,
                    }
                )
        return

    if entry.get("isMeta") is True or entry.get("isCompactSummary") is True:
        return
    # Harness recaps arrive on a non-user entry type. They are the densest
    # single line in most sessions ("Goal was X. Next: Y."), so they are kept —
    # but under their own heading. Filing them as prompts would attribute the
    # assistant's own summary to the user.
    if etype in SYSTEM_ENTRY_TYPES:
        recap = text_of(message)
        if recap and not AUTO_PROMPT_RE.match(recap):
            recap = redact(one_line(recap, cfg["max_recap_chars"]), cfg["redact"])
            if recap not in facts["recaps"]:
                facts["recaps"].append(recap)
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
    if AUTO_PROMPT_RE.match(prompt):
        return
    source = "shadow" if etype in SHADOW_ENTRY_TYPES else "user"
    facts["prompt_entries"].append(
        (source, redact(truncate(prompt, cfg["max_prompt_chars"]), cfg["redact"]))
    )


# --------------------------------------------------------------------------- #
# digest rendering
# --------------------------------------------------------------------------- #
# Which sections give way when a digest exceeds max_digest_chars, cheapest
# first. Anything absent from this table is never trimmed: without the
# AskUserQuestion answers there are no Decisions notes, without the errors there
# is no "막혔던 지점", and the commit list is the only record of what shipped.
#
# This exists because the old code rendered everything and then hard-cut the
# tail. Measured over 130 backfilled digests, 20 hit the cap and lost the
# *last* sections — commits 20/20, errors 17/20 — i.e. truncation ate exactly
# the densest facts while keeping 40 verbose prompts.
#
#   name        floor  keep_edges
#   floor       — never shrink below this many items
#   keep_edges  — drop from the middle. A session's first prompts say what it
#                 was for and its last say how it ended; the middle repeats.
TRIM_PLAN = [
    ("research", 0, False),
    ("subagents", 0, False),
    ("commands", 5, False),
    ("prompts", 8, True),
    ("recaps", 3, False),
    ("plans", 1, False),
    ("files", 10, False),
    # Last resort. These are the sections Decisions/막혔던 지점 are written from,
    # so they shrink only after everything else is at its floor — but shrinking
    # them with a "…N more" marker still beats the old blind tail cut, which
    # silently amputated whatever came last (usually the commit list).
    ("errors", 5, False),
    ("questions", 3, False),
]


def _section(name: str, title: str, items: list, more: str, dropped: int = 0) -> dict:
    return {
        "name": name,
        "title": title,
        "items": list(items),
        "more": more,
        "dropped": dropped,
    }


def _shrink(sec: dict, floor: int, keep_edges: bool) -> bool:
    """Remove one item. Returns False when the section is already at its floor.

    Deliberately does *not* report how many characters it freed: the caller must
    re-measure instead. Dropping the first item of a section adds a "…N more"
    marker line, so a caller that subtracts the item's length undercounts and
    stops trimming early — see fit_sections().
    """
    if len(sec["items"]) <= floor:
        return False
    idx = len(sec["items"]) // 2 if keep_edges else len(sec["items"]) - 1
    sec["items"].pop(idx)
    sec["dropped"] += 1
    return True


def render_section(sec: dict) -> list[str]:
    # A section trimmed down to nothing still gets its heading and marker: the
    # reader must be able to tell "there were 10 lookups, none shown" apart from
    # "there were no lookups".
    if not sec["items"] and not sec["dropped"]:
        return []
    lines = [sec["title"], ""]
    lines.extend(sec["items"])
    if sec["dropped"]:
        lines.append(sec["more"].format(n=sec["dropped"]))
    lines.append("")
    return lines


def fit_sections(head: list[str], sections: list[dict], budget: int) -> list[str]:
    """Spend the char budget on facts, not on the first N verbose prompts."""
    by_name = {s["name"]: s for s in sections}

    def total() -> int:
        body = [ln for s in sections for ln in render_section(s)]
        return len("\n".join(head + body))

    # total() is recomputed each round rather than tracked incrementally: the
    # first drop in a section *adds* a "…N more" marker line, so bookkeeping by
    # "freed = len(item)" drifts under the real size and the loop stops early —
    # which is how commits kept getting blind-cut even after the fitter existed.
    # Sections are capped (<=60 items), so the extra joins are immaterial.
    for name, floor, keep_edges in TRIM_PLAN:
        sec = by_name.get(name)
        if not sec:
            continue
        while total() > budget:
            if not _shrink(sec, floor, keep_edges):
                break
        if total() <= budget:
            break
    return head + [ln for s in sections for ln in render_section(s)]


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
    part = int(meta.get("part") or 0)
    if part:
        add(f"part: {part}")
    add(f'repo: "{repo}"')
    add(f'worktree: "{worktree}"')
    add(f'repo_name_guessed: {"true" if ident["guessed"] else "false"}')
    add(f'branch: "{branch}"')
    add(f'cwd: "{cwd}"')
    add(f'host: "{redact(host_name(cfg), cfg["redact"])}"')
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
    if part:
        add(
            f"> ⚠️ Part {part} of this session. An earlier digest was already synthesised "
            "while the session was still running, so this file re-reads the **whole** "
            "transcript — it overlaps the earlier note. Journal only what is new, and "
            "link the existing session note rather than writing a second one."
        )
        add("")
    if ident["guessed"]:
        add(
            f"> ⚠️ `repo: {repo}` is inferred from the directory name because "
            f"{ident.get('reason') or 'the repo could not be resolved'} (`{cwd}`). "
            "It may be a worktree's name, or a parent folder that merely contains "
            "repos. Verify before filing this under a project note."
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

    sections: list[dict] = []

    # Prompts are the one unbounded section (max_prompts × max_prompt_chars can
    # exceed the whole digest budget on its own), so the cap is applied through
    # the same middle-drop path the fitter uses — one behaviour, one message.
    prompt_sec = _section(
        "prompts",
        "## What I asked for (verbatim prompts)",
        [str(p) for p in facts["prompts"]] or ["_none captured_"],
        "…and {n} more prompts (trimmed from the middle to fit the size limit).",
    )
    while len(prompt_sec["items"]) > cfg["max_prompts"]:
        _shrink(prompt_sec, cfg["max_prompts"], True)
    sections.append(prompt_sec)

    recaps = facts["recaps"][: cfg["max_recaps"]]
    sections.append(
        _section(
            "recaps",
            "## Session recaps (assistant-generated, not user input)",
            [f"- {r}" for r in recaps],
            "- …and {n} more recaps",
            dropped=max(0, len(facts["recaps"]) - len(recaps)),
        )
    )

    # One item per question, not per line: trimming must never leave a "- **Q:**"
    # without the answer under it.
    q_items = []
    for q in facts["questions"]:
        block = ["- **Q:** " + str(q["q"])]
        if q["options"]:
            block.append("  - offered: " + ", ".join(q["options"]))
        block.append("  - **chose:** " + (q["answer"] or "(no answer recorded)"))
        q_items.append("\n".join(block))
    sections.append(
        _section(
            "questions",
            "## Explicit choices I made (AskUserQuestion)",
            q_items,
            "- …and {n} more questions",
        )
    )

    plan_items: list[str] = []
    for plan in facts["plans"][:5]:
        plan_items.append(f"```\n{plan}\n```")
    sections.append(
        _section(
            "plans",
            "## Plans approved (ExitPlanMode)",
            plan_items,
            "…and {n} more approved plans",
            dropped=max(0, len(facts["plans"][:5]) - len(plan_items)),
        )
    )

    file_items: list[str] = []
    for fp, n in files:
        rel = fp
        if repo_root and fp.startswith(repo_root):
            rel = fp[len(repo_root) :].lstrip("/")
        file_items.append(f"- `{rel}` ({n} edit{'s' if n > 1 else ''})")
    sections.append(
        _section(
            "files",
            "## Files touched",
            file_items or ["_no file edits_"],
            "- …and {n} more files",
            dropped=max(0, len(facts["files"]) - len(files)),
        )
    )

    cmds = facts["commands"][: cfg["max_commands"]]
    sections.append(
        _section(
            "commands",
            "## Notable commands run",
            [f"- `{c}`" for c in cmds],
            "- …and {n} more commands",
            dropped=max(0, len(facts["commands"]) - len(cmds)),
        )
    )

    errs = facts["errors"][: cfg["max_errors"]]
    # Built with concatenation rather than a nested f-string: the plugin runs on
    # whatever python3 the user has, and reusing the same quote inside an
    # f-string is a 3.12+ syntax (PEP 701).
    err_items = []
    for e in errs:
        target = " on `" + str(e["target"]) + "`" if e["target"] else ""
        mark = " _(inferred from output)_" if e.get("inferred") else ""
        err_items.append(
            "- **" + str(e["tool"]) + "**" + mark + target + ": " + str(e["detail"])
        )
    sections.append(
        _section(
            "errors",
            "## Errors / failures encountered",
            err_items,
            "- …and {n} more errors",
            dropped=max(0, len(facts["errors"]) - len(errs)),
        )
    )

    sections.append(
        _section(
            "commits",
            "## Commits during session",
            [f"- {line}" for line in commits.splitlines()[:20]] if commits else [],
            "",
        )
    )
    sections.append(
        _section("dirty", "## Uncommitted at session end", [f"- {dirty}"] if dirty else [], "")
    )
    sections.append(
        _section(
            "subagents",
            "## Subagents dispatched",
            [f"- {s}" for s in facts["subagents"][:15]],
            "- …and {n} more subagents",
        )
    )
    sections.append(
        _section(
            "research",
            "## Looked up externally",
            [f"- {r}" for r in dict.fromkeys(facts["research"])],
            "- …and {n} more lookups",
        )
    )

    text = "\n".join(fit_sections(out, sections, cfg["max_digest_chars"]))
    # Backstop: the protected sections alone can, in principle, exceed the
    # budget. Cutting is still better than an unbounded digest.
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

    ident = refine_identity(cwd, facts["cwd_counts"])
    meta = {"cwd": cwd, "session_id": session_id, "reason": reason, "ident": ident}

    if args.stdout:
        sys.stdout.write(build_digest(facts, meta, cfg))
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
    #
    # Dedupe is keyed on the *session*, not the filename. The filename embeds the
    # repo, and a session that renames or moves its checkout mid-flight (zot ->
    # internal-registry, observed once in 130 digests) produces a second file the
    # name-based check can never see — two digests, one session, synthesised
    # twice. Any file whose frontmatter carries this session_id is a twin.
    def digest_head(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:600]
        except OSError:
            return ""

    def turns_of(head: str) -> int:
        m = re.search(r"^turns: (\d+)$", head, re.M)
        return int(m.group(1)) if m else -1

    def twins() -> list:
        # Only base names, never "--partN" files: a part is a deliberate second
        # record of the same session, not a stale duplicate to be cleaned up.
        found = []
        for cand in out_dir.glob("*--" + slugify(session_id)[-8:] + ".md"):
            if cand == out_file:
                continue
            head = digest_head(cand)
            if re.search(r'^session_id: "' + re.escape(session_id) + r'"$', head, re.M):
                found.append(cand)
        return found

    def keep_reason(path: Path) -> str:
        head = digest_head(path)
        if not head:
            return ""
        if re.search(r"^status: processed$", head, re.M):
            return "processed"
        if turns_of(head) > facts["assistant_turns"]:
            return "richer"
        return ""

    def blocker() -> "tuple[str, Path | None]":
        for cand in ([out_file] if out_file.exists() else []) + twins():
            why = keep_reason(cand)
            if why:
                return why, cand
        return "", None

    part = 0
    why, holder = blocker()
    if why == "processed":
        # The session did not stop when the digest was synthesised. If the user
        # ran /dev-journal after a PreCompact capture and then kept working, the
        # SessionEnd digest used to be dropped here and everything after the
        # compact was never journaled at all. Keep the synthesised file
        # untouched, but record the rest of the session beside it.
        prior = turns_of(digest_head(holder))
        if facts["assistant_turns"] > prior:
            for n in range(2, 10):
                cand = out_file.with_name(out_file.stem + "--part" + str(n) + ".md")
                if not (cand.exists() and keep_reason(cand)):
                    out_file, part = cand, n
                    break
            else:
                log(f"{holder.name}: already synthesised and out of part slots; skipping")
                return 0
            log(
                f"{holder.name} was already synthesised at {prior} turns; the session "
                f"continued to {facts['assistant_turns']} — writing {out_file.name}"
            )
        else:
            log(f"{holder.name} already synthesised; leaving it alone")
            return 0
    elif why == "richer":
        log(f"{holder.name} has a richer digest ({turns_of(digest_head(holder))} turns "
            f"> {facts['assistant_turns']}); keeping it")
        return 0

    meta["part"] = part
    digest = build_digest(facts, meta, cfg)

    def write_blocked() -> bool:
        # A part file stands next to an already-synthesised base digest, so the
        # twin scan (which would find that base) must not apply to it.
        if part:
            return bool(out_file.exists() and keep_reason(out_file))
        return bool(blocker()[0])

    tmp_file = out_file.with_name(f".{out_file.name}.{os.getpid()}.tmp")
    tmp_file.write_text(digest, encoding="utf-8")
    # Re-check at the last moment: a concurrent capture may have landed a richer
    # digest while we were parsing.
    if write_blocked():
        tmp_file.unlink(missing_ok=True)
        return 0
    os.replace(tmp_file, out_file)
    # This digest supersedes any thinner twin left under an older repo name.
    if not part:
        for stale in twins():
            try:
                stale.unlink()
                log(f"removed stale twin of the same session: {stale.name}")
            except OSError:
                pass

    where = "vault" if in_vault else "local staging (vault_path not set)"
    log(f"wrote {out_file} ({len(digest)} chars) to {where}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never let a hook-spawned process die loudly
        log(f"unexpected failure: {exc!r}")
        sys.exit(0)
