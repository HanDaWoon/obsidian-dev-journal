#!/usr/bin/env python3
"""Cheap deterministic helpers for the dev-journal skill.

Keeps the LLM's job small: one call to find work, one call to close it out.

    journal_util.py doctor                 # config + install health check
    journal_util.py init --vault PATH      # write config, create folders
    journal_util.py pending [--limit N]    # unprocessed digests (JSON)
    journal_util.py cat FILE [FILE ...]    # concatenate digests for reading
    journal_util.py done FILE [FILE ...]   # mark digests processed
                       [--note PATH ...]   # ...and record which notes came out
    journal_util.py hold FILE [FILE ...]   # park a digest: not pending, not done
    journal_util.py unhold FILE [FILE ...] # put it back in the queue
    journal_util.py index                  # (re)write the vault's index note
    journal_util.py prune [--older-than N] # archive processed digests out of the vault
    journal_util.py backfill [--limit N]   # digest past sessions from ~/.claude/projects
    journal_util.py stats                  # what's in the journal so far
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

STATE_DIR = Path(os.path.expanduser("~/.claude/obsidian-dev-journal"))
CONFIG_PATH = Path(os.environ.get("OBSIDIAN_DEV_JOURNAL_CONFIG") or STATE_DIR / "config.json")
SCRIPT_DIR = Path(__file__).resolve().parent
SUBFOLDERS = ["_raw", "Sessions", "Decisions", "Learnings", "Projects", "Career"]
PLUGIN_NAME = "obsidian-dev-journal"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def base_dir(cfg: dict) -> Path | None:
    vault = (cfg.get("vault_path") or os.environ.get("OBSIDIAN_VAULT") or "").strip()
    if not vault:
        return None
    root = Path(os.path.expanduser(vault))
    return root / (cfg.get("base_folder") or "DevJournal") if root.is_dir() else None


def raw_dirs(cfg: dict) -> list[Path]:
    dirs = []
    base = base_dir(cfg)
    if base:
        dirs.append(base / "_raw")
    dirs.append(STATE_DIR / "staged")  # digests captured before setup finished
    return [d for d in dirs if d.is_dir()]


def frontmatter(path: Path) -> dict:
    meta: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            if fh.readline().strip() != "---":
                return meta
            for line in fh:
                if line.strip() == "---":
                    break
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
                if m:
                    meta[m.group(1)] = m.group(2).strip().strip('"')
    except Exception:
        pass
    return meta


# --------------------------------------------------------------------------- #
def set_status(path: Path, status: str, extra: "list[str] | None" = None,
               allow_from: "set[str] | None" = None) -> str:
    """Rewrite status inside the frontmatter block only.

    Substring replacement over the whole file can hit a captured prompt or a
    mid-body horizontal rule, so everything is anchored to the YAML block.
    Returns "" on success, else a reason.
    """
    if not path.is_file():
        return "missing"
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    if not m:
        return "no frontmatter"
    fm = m.group(1)
    cur = re.search(r"^status: (.*)$", fm, re.M)
    current = cur.group(1).strip() if cur else "unprocessed"
    if current == status:
        return "already " + status
    if allow_from is not None and current not in allow_from:
        return "status is '" + current + "'"
    block = "status: " + status
    for line in extra or []:
        block += "\n" + line
    if cur:
        fm = re.sub(r"^status: .*$", block, fm, count=1, flags=re.M)
    else:
        fm = block + "\n" + fm
    # Re-stamping must not pile up duplicate keys from an earlier run.
    for key in [e.split(":", 1)[0] for e in (extra or [])]:
        seen = 0
        out_lines = []
        for line in fm.splitlines():
            if line.startswith(key + ":"):
                seen += 1
                if seen > 1:
                    continue
            out_lines.append(line)
        fm = "\n".join(out_lines)
    # tmp + os.replace, same as the capture path: these files live in a synced
    # vault, so an in-place rewrite can be read half-written by Obsidian or the
    # sync client, and a crash mid-write loses the frontmatter outright.
    new_text = "---\n" + fm + "\n---\n" + text[m.end():]
    tmp = path.with_name("." + path.name + "." + str(os.getpid()) + ".tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return "write failed: " + str(exc)
    return ""


def cmd_init(args) -> int:
    vault = Path(os.path.expanduser(args.vault)).resolve()
    if not vault.is_dir():
        print(json.dumps({"ok": False, "error": f"not a directory: {vault}"}))
        return 1

    cfg = load_config()
    cfg["vault_path"] = str(vault)
    if args.folder is not None:
        cfg["base_folder"] = args.folder  # explicit flag always wins over old config
    cfg.setdefault("base_folder", "DevJournal")
    if args.min_prompts is not None:
        cfg["min_user_prompts"] = args.min_prompts
    if args.exclude:
        cfg["exclude_paths"] = args.exclude

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    base = vault / cfg["base_folder"]
    created = []
    base.mkdir(parents=True, exist_ok=True)
    for sub in SUBFOLDERS:
        d = base / sub
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d))

    # Move anything captured before setup into the vault. Never leave a staged
    # file behind — `pending` scans both dirs and a leftover means the same
    # session is synthesised twice, forever.
    moved = 0
    migration_failed = []
    staged = STATE_DIR / "staged"

    def turns_of(p: Path) -> int:
        m = re.search(r"^turns: (\d+)$", p.read_text(encoding="utf-8", errors="replace")[:600], re.M)
        return int(m.group(1)) if m else -1

    if staged.is_dir():
        for f in staged.glob("*.md"):
            # Every file is guarded, not just the collision branch: an
            # unwritable vault used to abort init here, leaving the folders
            # created, the config written and the staged digests half migrated.
            try:
                target = base / "_raw" / f.name
                if target.exists():
                    # Same session captured on both sides: keep the richer digest.
                    if turns_of(f) > turns_of(target):
                        target.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
                        moved += 1
                    f.unlink()
                    continue
                target.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
                f.unlink()
                moved += 1
            except Exception as exc:
                # Leave the staged copy in place — `pending` still finds it and
                # a later init can retry.
                migration_failed.append(f.name + " (" + str(exc) + ")")

    print(
        json.dumps(
            {
                "ok": True,
                "config": str(CONFIG_PATH),
                "base": str(base),
                "created": created,
                "index_note": write_index(base, cfg["base_folder"]),
                "migrated_digests": moved,
                "migration_failed": migration_failed,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def plugin_manifest(root: Path) -> dict:
    try:
        data = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def install_registry() -> dict:
    """installPath -> version, as Claude Code recorded it at install time."""
    reg: dict[str, str] = {}
    path = Path(os.path.expanduser("~/.claude/plugins/installed_plugins.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, entries in (data.get("plugins") or {}).items():
            if key.split("@")[0] != PLUGIN_NAME:
                continue
            for entry in entries or []:
                if isinstance(entry, dict) and entry.get("installPath"):
                    reg[str(Path(entry["installPath"]))] = entry.get("version") or ""
    except Exception:
        pass
    return reg


def find_plugin_installs() -> list[dict]:
    """Every copy of this plugin that could run, with its version.

    Hooks shipped by a plugin never appear in settings.json, so 'is the hook
    wired up' means 'is a copy installed and does its hooks.json exist'.

    Copies disagree in practice, which is the whole point of reporting them
    individually. A marketplace with a `directory` source runs the working tree,
    while the registry still points at the snapshot cached when the plugin was
    first added — and a second runtime (local agent mode) executes that
    snapshot. Observed here: hooks writing digests from 0.5.0 while
    installed_plugins.json said 0.3.1 and one runtime really used the old one.

    The snapshot was invisible before: its manifest sits six levels below
    ~/.claude/plugins (cache/<marketplace>/<plugin>/<version>/.claude-plugin/),
    and the crawl stopped at five — so doctor reported a healthy single install
    for exactly the situation it exists to catch.
    """
    here = SCRIPT_DIR.parent
    candidates = {here}
    for root in (
        Path(os.path.expanduser("~/.claude/plugins")),
        Path(os.path.expanduser("~/.claude/local-plugins")),
    ):
        if root.is_dir():
            for mf in root.glob("**/.claude-plugin/plugin.json"):
                if len(mf.parts) - len(root.parts) > 8:
                    continue  # bounded, but deep enough to reach the cache
                candidates.add(mf.parent.parent)

    registry = install_registry()
    candidates.update(Path(p) for p in registry)

    hits = []
    for c in sorted(candidates, key=str):
        registered = registry.get(str(c))
        manifest = plugin_manifest(c)
        # A registry entry whose directory is gone still matters: that is a
        # runtime pointed at nothing.
        if manifest.get("name") != PLUGIN_NAME and registered is None:
            continue
        hits.append(
            {
                "path": str(c),
                "version": manifest.get("version") or registered or "",
                "registered_version": registered,
                "exists": bool(manifest),
                "running": c == here,
                "hooks_json": (c / "hooks" / "hooks.json").is_file(),
            }
        )
    return hits


def cmd_doctor(_args) -> int:
    cfg = load_config()
    base = base_dir(cfg)

    installs = find_plugin_installs()
    hooks_ok = [i for i in installs if i["hooks_json"]]
    running = next((i for i in installs if i["running"]), None)
    versions = sorted({i["version"] for i in installs if i["version"]})
    stale = [i for i in installs if i["version"] and running and i["version"] != running["version"]]

    pending = 0
    for d in raw_dirs(cfg):
        pending += sum(
            1 for f in d.glob("*.md") if frontmatter(f).get("status", "unprocessed") == "unprocessed"
        )

    log_file = STATE_DIR / "logs" / "capture.log"
    last_capture = None
    if log_file.is_file():
        try:
            tail = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            last_capture = tail[-1][-300:] if tail else None
        except Exception:
            pass
    orphans = len(list((STATE_DIR / "pending").glob("*.json"))) if (STATE_DIR / "pending").is_dir() else 0

    report = {
        "config_path": str(CONFIG_PATH),
        "config_exists": CONFIG_PATH.is_file(),
        "vault_path": cfg.get("vault_path") or os.environ.get("OBSIDIAN_VAULT") or None,
        "vault_source": "config" if cfg.get("vault_path") else ("OBSIDIAN_VAULT env" if os.environ.get("OBSIDIAN_VAULT") else None),
        "vault_ok": base is not None,
        "journal_base": str(base) if base else None,
        "folders_present": [s for s in SUBFOLDERS if base and (base / s).is_dir()] if base else [],
        "staged_local": str(STATE_DIR / "staged"),
        "pending_digests": pending,
        "plugin_installs_found": installs,
        "plugin_version_running": running["version"] if running else None,
        "plugin_versions_seen": versions,
        # Two copies at different versions write different digests into one
        # vault, and which one runs depends on the runtime that fired the hook.
        "plugin_version_mismatch": [
            {"path": i["path"], "version": i["version"]} for i in stale
        ],
        "hooks_json_present": bool(hooks_ok),
        "hook_note": (
            "Plugin hooks load on Claude Code start — after installing, restart or /reload-plugins."
            if hooks_ok
            else "No installed copy with hooks/hooks.json found; capture will not fire."
        ),
        "version_note": (
            "Another copy is at a different version than the one answering this command. "
            "Whichever copy a runtime loads is the one that captures — run `/plugin update "
            + PLUGIN_NAME
            + "` (or reinstall) so they match."
            if stale
            else None
        ),
        "last_capture_log_line": last_capture,
        "stale_pending_payloads": orphans,
        "python": sys.version.split()[0],
        "capture_log": str(log_file),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_pending(args) -> int:
    cfg = load_config()
    items = []
    held = 0
    for d in raw_dirs(cfg):
        for f in sorted(d.glob("*.md")):
            meta = frontmatter(f)
            status = meta.get("status", "unprocessed")
            if status == "held":
                held += 1
            if status != "unprocessed":
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue  # deleted by a sync between glob and stat
            items.append(
                {
                    "path": str(f),
                    "repo": meta.get("repo", ""),
                    "host": meta.get("host", ""),
                    "started": meta.get("started", ""),
                    "duration": meta.get("duration", ""),
                    "turns": meta.get("turns", ""),
                    "files_touched": meta.get("files_touched", ""),
                    "errors_hit": meta.get("errors_hit", ""),
                    "bytes": size,
                }
            )
    items.sort(key=lambda i: i["started"])
    total = len(items)
    if args.limit:
        items = items[: args.limit]
    # A fixed batch of 8 means 17 passes on a 130-digest backlog. Scale the
    # suggestion with the queue, but keep it bounded — the cap exists so one
    # synthesis pass stays inside a comfortable context.
    suggested = 8 if total <= 24 else (12 if total <= 60 else 16)
    print(
        json.dumps(
            {
                "count": total,
                "returned": len(items),
                "held": held,
                "suggested_batch": min(suggested, total) or 0,
                "digests": items,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def cmd_cat(args) -> int:
    for p in args.files:
        path = Path(p)
        if not path.is_file():
            print(f"\n<!-- missing: {p} -->\n")
            continue
        print(f"\n===== DIGEST {path.name} =====\n")
        print(path.read_text(encoding="utf-8", errors="replace"))
    return 0


def cmd_done(args) -> int:
    """Mark digests processed, optionally recording the notes they produced.

    The back-reference matters because synthesis is not 1:1 — one Orca wave of
    15 dispatched workers is honestly one session note. Without `notes:` the
    only record of "which digest became which note" is prose the model happened
    to type into the note body.
    """
    stamp = datetime.now().strftime("%Y-%m-%d")
    notes = [str(n) for n in (args.note or [])]
    extra = ["processed: " + stamp]
    if notes:
        extra.append("notes: [" + ", ".join('"' + n.replace('"', "") + '"' for n in notes) + "]")

    updated, already, skipped = [], [], []
    for p in args.files:
        # A parked digest must not be marked processed by accident: `pending`
        # already hides it, so anything reaching here is either an explicit path
        # or a stale one. Refuse and say so — unhold is one command away.
        why = set_status(Path(p), "processed", extra, allow_from={"unprocessed"})
        if not why:
            updated.append(p)
        elif why.startswith("already"):
            already.append(p)
        else:
            skipped.append(p + " (" + why + ")")
    print(
        json.dumps(
            {"marked": updated, "already_processed": already, "skipped": skipped, "notes": notes},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def cmd_hold(args) -> int:
    """Park a digest without synthesising it.

    Two states (unprocessed/processed) could not express "deliberately deferred"
    — a digest whose repo lives on another machine stayed in `pending` forever,
    indistinguishable from a backlog.
    """
    stamp = datetime.now().strftime("%Y-%m-%d")
    extra = ["held: " + stamp]
    if args.reason:
        extra.append('hold_reason: "' + args.reason.replace('"', "") + '"')
    held, skipped = [], []
    for p in args.files:
        why = set_status(Path(p), "held", extra, allow_from={"unprocessed", "held"})
        (held if not why else skipped).append(p if not why else p + " (" + why + ")")
    print(json.dumps({"held": held, "skipped": skipped, "reason": args.reason},
                     indent=2, ensure_ascii=False))
    return 0


def cmd_unhold(args) -> int:
    back, skipped = [], []
    for p in args.files:
        why = set_status(Path(p), "unprocessed", [], allow_from={"held"})
        (back if not why else skipped).append(p if not why else p + " (" + why + ")")
    print(json.dumps({"returned_to_queue": back, "skipped": skipped},
                     indent=2, ensure_ascii=False))
    return 0


CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.){1,512})"')


def transcript_cwd(path: Path) -> str:
    """The directory a transcript belongs to. Each git worktree has its own cwd,
    so this is what distinguishes worktree sessions from main-checkout ones.

    Modal, not first — same rule as capture_session.resolve_cwd(). This used to
    read the first cwd while capture_session kept the last, so `backfill --repo`
    and the digest it produced could disagree about the same session. Scanned
    with a regex rather than json.loads: backfill calls this for every candidate
    transcript and some are tens of megabytes.
    """
    counts: dict[str, int] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = CWD_RE.search(line)
                if m:
                    cwd = m.group(1).replace("\\/", "/").replace('\\"', '"').replace("\\\\", "\\")
                    counts[cwd] = counts.get(cwd, 0) + 1
    except Exception:
        pass
    if not counts:
        return ""
    order = {c: i for i, c in enumerate(counts)}
    return max(counts, key=lambda c: (counts[c], -order[c]))


def cmd_backfill(args) -> int:
    """Digest historical transcripts that predate the hook install.

    Claude Code stores one project directory per working directory, and a git
    worktree is a different working directory — so worktree sessions live in
    their own project folders. Scanning all of ~/.claude/projects therefore picks
    up every worktree automatically; use --repo to filter, since the canonical
    repo name is only recoverable when the checkout still exists.
    """
    projects = Path(os.path.expanduser("~/.claude/projects"))
    if not projects.is_dir():
        print(json.dumps({"ok": False, "error": f"not found: {projects}"}))
        return 1

    cutoff = 0.0
    if args.since_days:
        cutoff = datetime.now().timestamp() - args.since_days * 86400

    candidates = []
    for f in projects.rglob("*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        if cutoff and st.st_mtime < cutoff:
            continue
        candidates.append((st.st_mtime, f))
    candidates.sort(reverse=True)

    if args.dry_run or args.repo:
        enriched = []
        for mtime, f in candidates:
            cwd = transcript_cwd(f)
            enriched.append((mtime, f, cwd))
        if args.repo:
            needle = args.repo.lower()
            enriched = [e for e in enriched if needle in (e[2] or "").lower()]
        candidates = [(m, f) for m, f, _ in enriched]
        if args.dry_run:
            rows = []
            for m, f, cwd in enriched[: args.limit or len(enriched)]:
                try:
                    mb = round(f.stat().st_size / 1e6, 2)
                except OSError:
                    continue  # deleted between glob and stat
                rows.append(
                    {
                        "transcript": str(f),
                        "cwd": cwd,
                        "modified": datetime.fromtimestamp(m).strftime("%Y-%m-%d %H:%M"),
                        "mb": mb,
                    }
                )
            print(
                json.dumps(
                    {"ok": True, "dry_run": True, "matched": len(enriched), "would_process": rows},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0

    if args.limit:
        candidates = candidates[: args.limit]

    done, skipped, failed, cwds = [], [], [], {}
    for _mtime, f in candidates:
        cwd = transcript_cwd(f)
        cwds[cwd or "(unknown)"] = cwds.get(cwd or "(unknown)", 0) + 1
        res = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "capture_session.py"),
                "--transcript",
                str(f),
                "--session-id",
                f.stem,
                "--cwd",
                cwd or str(Path.home()),
            ],
            capture_output=True,
            text=True,
        )
        tail = (res.stdout or "").strip().splitlines()
        note = tail[-1] if tail else ""
        if res.returncode != 0:
            failed.append({"file": f.name, "error": (res.stderr or "")[-200:]})
        elif "wrote" in note:
            done.append(f.name)
        else:
            skipped.append({"file": f.name, "why": note[-160:]})

    print(
        json.dumps(
            {
                "ok": True,
                "scanned": len(candidates),
                "digests_written": len(done),
                "written": done,
                "skipped": skipped,
                "failed": failed,
                "working_dirs_seen": cwds,
                "hint": "Distinct working dirs above include git worktrees. "
                "Run `pending` next, then the dev-journal skill.",
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


INDEX_NOTE = """# Dev Journal

> `obsidian-dev-journal` 이 관리하는 볼트의 진입점. Dataview 플러그인이 필요하다.
> 이 파일은 `journal_util.py index` 로 다시 만들 수 있다.

## 최근 세션
```dataview
TABLE repo, duration, date
FROM #dev-journal AND "{base}/Sessions"
SORT date DESC
LIMIT 25
```

## 결정 로그
```dataview
TABLE repo, status, date
FROM #decision
SORT date DESC
```

## 프로젝트
```dataview
TABLE repo, sessions, last_updated
FROM #project
SORT last_updated DESC
```

## 흔들리는 지식 (복습 필요)
```dataview
LIST
FROM #learning
WHERE confidence = "shaky"
SORT last_updated ASC
```

## 최근 갱신된 학습
```dataview
TABLE last_updated, confidence
FROM #learning
SORT last_updated DESC
LIMIT 15
```

## 처리 대기 중인 다이제스트
```dataview
LIST
FROM "{base}/_raw"
WHERE status = "unprocessed"
```

## 보류한 다이제스트
```dataview
TABLE hold_reason, held
FROM "{base}/_raw"
WHERE status = "held"
```
"""


def write_index(base: Path, folder: str, force: bool = False) -> "str | None":
    """The index is the vault's entry point. 89 session notes with no way in is
    a filing cabinet nobody opens."""
    target = base / "DevJournal.md"
    if target.exists() and not force:
        return None
    target.write_text(INDEX_NOTE.replace("{base}", folder), encoding="utf-8")
    return str(target)


def cmd_index(args) -> int:
    cfg = load_config()
    base = base_dir(cfg)
    if not base:
        print(json.dumps({"ok": False, "error": "vault not configured; run init"}))
        return 1
    written = write_index(base, cfg.get("base_folder") or "DevJournal", force=args.force)
    print(
        json.dumps(
            {
                "ok": True,
                "written": written,
                "note": None if written else "index already exists; pass --force to overwrite",
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def cmd_prune(args) -> int:
    """Retire digests that have already been synthesised.

    `_raw/` is an input queue, but nothing ever emptied it: 133 processed
    digests (846KB of verbatim prompts and command lines) accumulate inside the
    vault, where Obsidian indexes them into search and the graph forever. The
    notes are the product; the digests are the receipt.

    Archiving moves them *out of the vault* — that is the point, so an Obsidian
    search stops matching them — while keeping them on disk and reversible.
    Only `processed` files are eligible: `held` and `unprocessed` are still
    work in hand.
    """
    cfg = load_config()
    dirs = raw_dirs(cfg)
    if not dirs:
        print(json.dumps({"ok": False, "error": "no digest folder found; run init"}))
        return 1

    cutoff = datetime.now().timestamp() - max(0, args.older_than) * 86400
    archive = STATE_DIR / "archive"
    picked, kept = [], 0
    for d in dirs:
        for f in sorted(d.glob("*.md")):
            meta = frontmatter(f)
            if meta.get("status") != "processed":
                kept += 1
                continue
            # Age from the day it was synthesised, not the day it was captured:
            # a backfilled March session processed yesterday is one day old as
            # far as "do I still need this receipt" goes.
            when = None
            try:
                when = datetime.strptime(meta.get("processed", ""), "%Y-%m-%d").timestamp()
            except ValueError:
                pass
            if when is None:
                try:
                    when = f.stat().st_mtime
                except OSError:
                    continue
            if when > cutoff:
                kept += 1
                continue
            picked.append(f)

    moved, deleted, failed = [], [], []
    if not args.dry_run:
        if not args.delete:
            archive.mkdir(parents=True, exist_ok=True)
        for f in picked:
            try:
                if args.delete:
                    f.unlink()
                    deleted.append(f.name)
                else:
                    target = archive / f.name
                    n = 2
                    while target.exists():
                        target = archive / (f.stem + "-" + str(n) + f.suffix)
                        n += 1
                    f.replace(target)
                    moved.append(str(target))
            except OSError as exc:
                failed.append(f.name + " (" + str(exc) + ")")

    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": bool(args.dry_run),
                "older_than_days": args.older_than,
                "eligible": len(picked),
                "kept": kept,
                "archived_to": None if args.delete else str(archive),
                "archived": moved,
                "deleted": deleted,
                "failed": failed,
                "candidates": [f.name for f in picked] if args.dry_run else [],
                "note": "held and unprocessed digests are never pruned",
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def cmd_stats(_args) -> int:
    cfg = load_config()
    base = base_dir(cfg)
    if not base:
        print(json.dumps({"ok": False, "error": "vault not configured; run init"}))
        return 1
    out = {"base": str(base)}
    for sub in SUBFOLDERS:
        d = base / sub
        out[sub] = len(list(d.glob("*.md"))) if d.is_dir() else 0
    raw = base / "_raw"
    if raw.is_dir():
        states: dict[str, int] = {}
        # Host counts only become interesting after two machines' vaults are
        # merged; digests captured before the field existed group under "".
        hosts: dict[str, int] = {}
        for f in raw.glob("*.md"):
            meta = frontmatter(f)
            s = meta.get("status", "unprocessed")
            states[s] = states.get(s, 0) + 1
            h = meta.get("host", "")
            hosts[h] = hosts.get(h, 0) + 1
        out["_raw_by_status"] = states
        if len(hosts) > 1 or (hosts and "" not in hosts):
            out["_raw_by_host"] = hosts
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--vault", required=True)
    p.add_argument("--folder", default=None, help="journal folder in the vault (default: DevJournal, kept on re-init)")
    p.add_argument("--min-prompts", type=int, default=None)
    p.add_argument("--exclude", nargs="*", default=None)
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("doctor"); p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("pending")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(fn=cmd_pending)

    p = sub.add_parser("cat")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_cat)

    p = sub.add_parser("done")
    p.add_argument("files", nargs="+")
    p.add_argument("--note", action="append", default=None,
                   help="note this digest produced; repeatable, applies to all listed digests")
    p.set_defaults(fn=cmd_done)

    p = sub.add_parser("hold", help="park a digest: excluded from pending, not processed")
    p.add_argument("files", nargs="+")
    p.add_argument("--reason", default="", help="why it is parked")
    p.set_defaults(fn=cmd_hold)

    p = sub.add_parser("unhold")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_unhold)

    p = sub.add_parser("index", help="(re)write the vault index note")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("backfill")
    p.add_argument("--limit", type=int, default=25, help="0 = no limit")
    p.add_argument("--since-days", type=int, default=0, help="only sessions this recent")
    p.add_argument("--repo", default="", help="substring filter on the session's working dir")
    p.add_argument("--dry-run", action="store_true", help="list what would be digested")
    p.set_defaults(fn=cmd_backfill)

    p = sub.add_parser("prune", help="retire already-synthesised digests out of the vault")
    p.add_argument("--older-than", type=int, default=90, help="days since synthesis (default 90)")
    p.add_argument("--delete", action="store_true", help="delete instead of archiving")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
