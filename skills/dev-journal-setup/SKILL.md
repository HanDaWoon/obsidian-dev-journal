---
name: dev-journal-setup
description: >
  Set up or repair the Obsidian dev journal. Use when the user says they want to set up
  the dev journal, connect their Obsidian vault, change the vault path or journal folder,
  asks "why isn't my journal being written", asks to backfill past Claude Code sessions
  into Obsidian, or wants to check that the session-capture hook is working.
allowed-tools: Bash, Read, Write, Edit, Glob, AskUserQuestion
---

# Dev Journal — Setup & Diagnostics

Configure the capture pipeline, verify it works, and optionally backfill history.

`${CLAUDE_PLUGIN_ROOT}` is this plugin's directory. All scripts live in
`${CLAUDE_PLUGIN_ROOT}/scripts/`. Run them with `python3`.

## How the system works (explain this if asked)

1. A `SessionEnd` hook fires when a Claude Code session ends. It writes the hook
   payload to disk and returns in milliseconds — it never blocks the user.
2. A detached background process (`capture_session.py`) reduces the session
   transcript to a compact digest of facts: verbatim prompts, explicit choices,
   approved plans, files edited, notable commands, errors hit, commits made.
   **No LLM runs here, so a coding session costs zero extra tokens.**
3. The digest lands in `<vault>/DevJournal/_raw/` with `status: unprocessed`.
4. Later, the `dev-journal` skill reads a batch of digests in one pass and writes
   the real notes: session log, decisions, learnings, project knowledge, career
   achievements. One LLM pass per batch instead of one per session.

## Step 1 — find the vault

Never guess the vault path. Try to detect candidates first:

```bash
ls -d ~/Documents/*/.obsidian ~/*/.obsidian ~/Library/Mobile\ Documents/iCloud~md~obsidian/Documents/*/.obsidian 2>/dev/null | sed 's|/\.obsidian$||'
cat ~/Library/Application\ Support/obsidian/obsidian.json 2>/dev/null
```

Present what was found with AskUserQuestion (each candidate as an option). If
nothing is found, ask the user to paste the absolute path. A valid vault is a
directory that contains `.obsidian/`.

## Step 2 — write the config and create folders

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/journal_util.py" init --vault "<ABSOLUTE_VAULT_PATH>"
```

This writes `~/.claude/obsidian-dev-journal/config.json`, creates the folder
tree, and migrates any digests that were captured before setup finished.

If the user works on more than one machine, mention `host_name` in the config:
each digest records which machine it came from, it cannot be reconstructed
later, and it is what makes merging two machines' vaults possible down the road.
Default is the hostname's first label, which is usually fine.

Optional flags: `--folder <name>` (default `DevJournal`),
`--min-prompts N` (skip sessions shorter than N prompts, default 2),
`--exclude <substr> ...` (never journal work under these paths — use for client
or private repos).

## Step 3 — verify

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/journal_util.py" doctor
```

Check the report: `vault_ok: true`, folders present, `hooks_json_present: true`,
and `plugin_installs_found` non-empty. If `plugin_version_mismatch` is not empty,
say so before anything else: a second copy of the plugin at another version is
installed, whichever copy a runtime loads is the one that captures, and the two
write different digests into the same vault. `/plugin update obsidian-dev-journal`
(or reinstall) is the fix. Plugin hooks load when Claude Code starts,
so after a fresh install tell the user to restart (or `/reload-plugins`) and that
the *next* session will be the first one captured. `last_capture_log_line` shows
whether capture has ever fired; `stale_pending_payloads` above ~5 suggests the
background process is failing — check the capture log.

Then prove the parser works against a real transcript, without touching the vault:

```bash
ls -t ~/.claude/projects/*/*.jsonl | head -1
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/capture_session.py" --transcript "<that file>" --force --stdout | head -60
```

Show the user the first part of that digest so they can see what will be captured.

## Step 4 — offer a backfill

Past sessions are still on disk under `~/.claude/projects/`. Always preview
before writing:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/journal_util.py" backfill --dry-run --limit 40
```

Show the user the working directories found, then run it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/journal_util.py" backfill --limit 40
```

Flags: `--since-days N`, `--repo <substring of the working dir>`,
`--limit 0` for everything, `--dry-run`.

Then suggest running the `dev-journal` skill to synthesise them, in batches of
~8 digests so no single pass gets too long.

### Git worktrees

Claude Code keys its transcript storage by working directory, and a git worktree
*is* a separate working directory — so each worktree has its own folder under
`~/.claude/projects/`. Backfill scans all of them, so **worktree sessions are
included automatically**; the `working_dirs_seen` map in the output shows every
checkout that contributed.

Attribution is resolved through `git rev-parse --git-common-dir`, so a session in
`../feat-x` or `.worktrees/hotfix` is attributed to the real repository, with the
worktree recorded separately in the `worktree:` field. All of a repo's worktrees
therefore feed one `Projects/<repo>.md`.

The one gap: if a worktree has since been deleted, git can no longer be asked
which repo it belonged to. Those digests get `repo_name_guessed: true` and a
warning, and the repo name falls back to the directory name. Point this out when
it appears in a backfill — the user may want to correct the `repo:` field before
synthesis.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No digests appear | `cat ~/.claude/obsidian-dev-journal/logs/capture.log` and `logs/hook.log` |
| Digests in `~/.claude/obsidian-dev-journal/staged/` | Vault was not configured when they were captured — rerun `init`, which migrates them |
| `python3 not found` in hook.log | Install Python 3, or point the hook at an interpreter that exists |
| Digest is nearly empty | Transcript format may have changed in a Claude Code update; inspect a few lines with `head -3 <transcript> \| python3 -m json.tool` and report which keys moved |
| Sessions being skipped | Below the signal threshold — lower `min_user_prompts` in the config |
| Digests inconsistent between sessions (some missing sections) | `doctor` → `plugin_version_mismatch`. Different runtimes can load different copies of the plugin |
| Secrets appearing in digests | `redact` catches `key=`/`token:` patterns only. Add the repo to `exclude_paths` for anything sensitive |

## Privacy note to surface once during setup

Digests contain verbatim prompts, file paths, and command lines from the user's
work, stored as plain files in their vault. If the vault syncs to a third-party
service, say so plainly and mention `exclude_paths` before finishing setup.
