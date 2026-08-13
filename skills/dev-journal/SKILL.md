---
name: dev-journal
description: >
  Turn captured Claude Code sessions into Obsidian notes — decision log, learnings,
  codebase knowledge, and resume-ready achievement bullets. Use when the user says
  "write my dev journal", "정리해줘" about recent coding sessions, asks what they decided
  or learned this week, wants their Obsidian journal updated or caught up, or asks to
  process pending session digests.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, AskUserQuestion
---

# Dev Journal — Synthesis

Read pending raw session digests and write durable Obsidian notes from them.
This is the only step that spends tokens, so it batches: many digests, one pass.

Scripts live in `${CLAUDE_PLUGIN_ROOT}/scripts/`. Run with `python3`.

## Vault layout

```
<vault>/DevJournal/
├── _raw/          machine digests (input; never hand-edit)
├── Sessions/      one note per session — the narrative record
├── Decisions/     one note per real decision — the ADR-lite log
├── Learnings/     one note per topic, appended over time
├── Projects/      one living note per repo — codebase knowledge
└── Career/        Achievements.md — append-only impact bullets
```

## Procedure

### 1. Find the work

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/journal_util.py" pending
```

If `count` is 0: report that everything is caught up, show
`journal_util.py stats`, and stop. Do not invent notes.

If nothing is configured (`vault_ok: false` from `doctor`), run the
`dev-journal-setup` skill instead.

### 2. Read a batch

Use the `suggested_batch` from `pending` (it scales with the backlog: 8 normally,
up to 16 when a large backfill is queued). Prefer the oldest unprocessed ones.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/journal_util.py" cat <path1> <path2> ...
```

Before writing to `Projects/` or `Learnings/`, read the existing note for that
repo or topic so you extend it instead of duplicating it:

```bash
ls "<vault>/DevJournal/Projects" "<vault>/DevJournal/Learnings"
```

### 3. Judge before you write

The digests are raw facts. Most sessions do not contain a real decision or a
real learning, and writing one anyway is how this vault becomes landfill.

Apply these bars:

- **Decision** — a fork with a live alternative that would have produced a
  different codebase. `AskUserQuestion` answers, approved `ExitPlanMode` plans,
  and prompts where the user overruled an approach are the strongest signals.
  "Use 4 spaces" is not a decision. "Chose polling over websockets because the
  proxy drops idle connections" is.
- **Learning** — something that will still be true next month and that the user
  did not already know. Error → root cause → fix chains are the richest source.
  A typo you fixed is not a learning.
- **Achievement** — an outcome with a scope and a result, not an activity. If you
  cannot name what changed for a user, a system, or the team, do not write one.
- **Project knowledge** — a fact about the codebase that would save the next
  person 20 minutes: architecture, invariants, gotchas, conventions, ownership.

When a session yields nothing for a category, write nothing for that category
and say so in the session note. Zero notes from a thin session is a correct
outcome.

### 4. Write the notes

Follow `references/note-templates.md` exactly for frontmatter and headings —
consistent frontmatter is what makes these notes queryable in Obsidian
Dataview.

Rules that matter:

- Write a `Sessions/` note for each **unit of work**, and link every digest it
  covers in the note's 흔적/trace section. Usually that is one note per digest.
  **Merge digests into one note when they are one unit of work** — filing 15
  notes for 15 slices of the same thing buries the signal:
  - **same `session_id`, different files** — one session whose checkout was
    renamed mid-flight (newer captures dedupe this, older digests may not)
  - **dispatched workers of one wave** — Orca/subagent sessions that start at
    the same timestamp in the same repo, each holding one slice. Name the wave,
    list what each worker produced.
  - **trivial harness invocations** — e.g. commit-message generation: several
    1-turn digests with no file edits. One stub note, all digests listed.
  Never merge across repos, and never merge sessions that are days apart.
  A merged note must still name every digest it consumed.
- **Worktrees**: file `Projects/` and `Learnings/` notes under the digest's
  `repo` field, never under `worktree`. Sessions from `main`, `feat-x`, and
  `.worktrees/hotfix` of one repo all feed the same `Projects/<repo>.md`.
  Mention the worktree in the session note only (it explains parallel work),
  and set `worktree:` in that note's frontmatter when present.
- Copy the digest's `host:` into the session note's frontmatter verbatim (omit
  the line when the digest has none). Processed digests get archived out of the
  vault, so the session note becomes the only record of which machine ran it.
- If a digest has `repo_name_guessed: true`, its checkout no longer exists and
  the repo name may actually be a worktree's directory name. Do not create a new
  `Projects/` note from it — check whether an existing project note is the real
  home, and ask the user (`AskUserQuestion`) if it is ambiguous.
- If a digest has `part: N` in its frontmatter, the session kept running after an
  earlier digest of it was already synthesised. The part file re-reads the
  **whole** transcript, so it overlaps a note that already exists. Find that note
  (the earlier digest's `notes:` field names it), extend it with what is new, and
  do not write a second session note for the same session.
- Filenames: `Sessions/YYYY-MM-DD-<repo>-<slug>.md`,
  `Decisions/YYYY-MM-DD-<slug>.md`, `Learnings/<Topic Name>.md`,
  `Projects/<repo>.md`.
- Cross-link with wikilinks in both directions. A decision links to its session;
  the session lists its decisions.
- `Learnings/` and `Projects/` notes are **merged, not replaced**: read, then
  append a dated entry or revise a stale line. Never clobber existing content.
- `Career/Achievements.md` is append-only. New bullets go under the current
  month's heading.
- Write in the user's own language. If their prompts are in Korean, write the
  notes in Korean.
- Be specific and quote the evidence. Reference actual file paths, error
  messages, and commands from the digest. Never write filler like "improved code
  quality".
- Do not speculate beyond the digest. If a decision's rationale was never
  stated, write `rationale: 미기록` (or "not recorded") rather than inventing one.

### 5. Close out

Mark exactly the digests you processed, recording which notes came out of them.
`--note` is repeatable and applies to every digest listed, which is what you
want for a merged note:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/journal_util.py" done <path1> <path2> \
  --note "<vault>/DevJournal/Sessions/2026-08-11-repo-wave.md"
```

If the user asks to defer a digest rather than synthesise it (its repo lives on
another machine, it is out of scope), park it instead of leaving it pending
forever — a parked digest drops out of `pending` and keeps its reason:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/journal_util.py" hold <path> --reason "..."
```

Then report to the user, briefly:

- how many sessions were processed, how many are still pending, and how many
  are parked (`held` in the `pending` output)
- the notes created vs. updated, as clickable paths
- the achievement bullets added, quoted in full — this is the part they care
  about most and will want to edit
- anything you deliberately skipped, and why

If digests remain pending, offer to run another batch.

On the very first pass in a vault, also create the entry point once:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/journal_util.py" index
```

`_raw/` is a queue, not an archive. If it has grown past a few hundred files,
offer to retire the ones that have already become notes — they move out of the
vault (reversibly) so Obsidian stops indexing them. Never run this unasked:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/journal_util.py" prune --older-than 90 --dry-run
```

## Achievement bullets

These land in a resume. Draft them as claims a hiring manager can interrogate:

> **&lt;Action verb&gt; &lt;what&gt; &lt;in what system&gt;, &lt;result&gt;** — with the number if
> the digest contains one, and the mechanism if it does not.

Good: "Cut CI wall time from 14m to 6m by parallelising the integration suite
and caching the Docker build layer."
Bad: "Worked on CI improvements."

Never fabricate a metric. When the digest has no number, write the mechanism and
scope, and append `[metric needed]` so the user knows to fill it in.

## Reference

`references/note-templates.md` — exact templates for all five note types, plus
Dataview queries to drop into the vault's index note.
