#!/usr/bin/env python3
"""Cheap deterministic helpers for the dev-journal skill.

Keeps the LLM's job small: one call to find work, one call to close it out.

    journal_util.py doctor                 # config + install health check
    journal_util.py init --vault PATH      # write config, create folders
    journal_util.py pending [--limit N]    # unprocessed digests (JSON)
    journal_util.py cat FILE [FILE ...]    # concatenate digests for reading
    journal_util.py done FILE [FILE ...]   # mark digests processed
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
    for sub in SUBFOLDERS:
        d = base / sub
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d))

    # Move anything captured before setup into the vault. Never leave a staged
    # file behind — `pending` scans both dirs and a leftover means the same
    # session is synthesised twice, forever.
    moved = 0
    staged = STATE_DIR / "staged"
    if staged.is_dir():
        for f in staged.glob("*.md"):
            target = base / "_raw" / f.name
            if target.exists():
                # Same session captured on both sides: keep the richer digest.
                def turns_of(p: Path) -> int:
                    m = re.search(r"^turns: (\d+)$", p.read_text(encoding="utf-8", errors="replace")[:600], re.M)
                    return int(m.group(1)) if m else -1
                try:
                    if turns_of(f) > turns_of(target):
                        target.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
                        moved += 1
                except Exception:
                    pass
                f.unlink()
                continue
            target.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            f.unlink()
            moved += 1

    print(
        json.dumps(
            {
                "ok": True,
                "config": str(CONFIG_PATH),
                "base": str(base),
                "created": created,
                "migrated_digests": moved,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def find_plugin_installs() -> list[str]:
    """Locate installed copies of this plugin. Hooks shipped by a plugin never
    appear in settings.json, so 'is the hook wired up' means 'is the plugin
    installed and does its hooks.json exist'."""
    hits = []
    # The running copy (journal_util.py lives in <plugin>/scripts/).
    here = SCRIPT_DIR.parent
    candidates = {here}
    for root in (
        Path(os.path.expanduser("~/.claude/plugins")),
        Path(os.path.expanduser("~/.claude/local-plugins")),
    ):
        if root.is_dir():
            for mf in root.glob("**/.claude-plugin/plugin.json"):
                if len(mf.parts) - len(root.parts) > 5:
                    continue  # don't crawl deep trees
                candidates.add(mf.parent.parent)
    for c in candidates:
        mf = c / ".claude-plugin" / "plugin.json"
        try:
            if json.loads(mf.read_text(encoding="utf-8")).get("name") == "obsidian-dev-journal":
                hits.append(str(c))
        except Exception:
            continue
    return sorted(hits)


def cmd_doctor(_args) -> int:
    cfg = load_config()
    base = base_dir(cfg)

    installs = find_plugin_installs()
    hooks_ok = [p for p in installs if (Path(p) / "hooks" / "hooks.json").is_file()]

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
        "hooks_json_present": bool(hooks_ok),
        "hook_note": (
            "Plugin hooks load on Claude Code start — after installing, restart or /reload-plugins."
            if hooks_ok
            else "No installed copy with hooks/hooks.json found; capture will not fire."
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
    for d in raw_dirs(cfg):
        for f in sorted(d.glob("*.md")):
            meta = frontmatter(f)
            if meta.get("status", "unprocessed") != "unprocessed":
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue  # deleted by a sync between glob and stat
            items.append(
                {
                    "path": str(f),
                    "repo": meta.get("repo", ""),
                    "started": meta.get("started", ""),
                    "duration": meta.get("duration", ""),
                    "turns": meta.get("turns", ""),
                    "files_touched": meta.get("files_touched", ""),
                    "errors_hit": meta.get("errors_hit", ""),
                    "bytes": size,
                }
            )
    items.sort(key=lambda i: i["started"])
    if args.limit:
        items = items[: args.limit]
    print(json.dumps({"count": len(items), "digests": items}, indent=2, ensure_ascii=False))
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
    """Flip status→processed inside the frontmatter block only. Substring
    replacement over the whole file can hit a captured prompt or a mid-body
    horizontal rule; anchor everything to the YAML block."""
    stamp = datetime.now().strftime("%Y-%m-%d")
    updated, already, skipped = [], [], []
    for p in args.files:
        path = Path(p)
        if not path.is_file():
            skipped.append(p)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
        if not m:
            skipped.append(f"{p} (no frontmatter)")
            continue
        fm = m.group(1)
        if re.search(r"^status: processed$", fm, re.M):
            already.append(p)
            continue
        if re.search(r"^status: ", fm, re.M):
            fm = re.sub(r"^status: .*$", f"status: processed\nprocessed: {stamp}", fm, count=1, flags=re.M)
        else:
            fm = f"status: processed\nprocessed: {stamp}\n" + fm
        path.write_text(f"---\n{fm}\n---\n" + text[m.end() :], encoding="utf-8")
        updated.append(p)
    print(
        json.dumps(
            {"marked": updated, "already_processed": already, "skipped": skipped},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def transcript_cwd(path: Path) -> str:
    """First cwd recorded in a transcript. Each git worktree has its own cwd, so
    this is what distinguishes worktree sessions from main-checkout ones."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 200:
                    break
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("cwd"):
                    return obj["cwd"]
    except Exception:
        pass
    return ""


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
        for f in raw.glob("*.md"):
            s = frontmatter(f).get("status", "unprocessed")
            states[s] = states.get(s, 0) + 1
        out["_raw_by_status"] = states
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
    p.set_defaults(fn=cmd_done)

    p = sub.add_parser("backfill")
    p.add_argument("--limit", type=int, default=25, help="0 = no limit")
    p.add_argument("--since-days", type=int, default=0, help="only sessions this recent")
    p.add_argument("--repo", default="", help="substring filter on the session's working dir")
    p.add_argument("--dry-run", action="store_true", help="list what would be digested")
    p.set_defaults(fn=cmd_backfill)

    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
