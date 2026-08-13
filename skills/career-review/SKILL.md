---
name: career-review
description: >
  Turn the accumulated dev journal into resume bullets, a performance-review self-assessment,
  or a growth retrospective. Use when the user asks what they accomplished this quarter or year,
  wants resume or CV bullets from their work, needs material for a performance review or
  promotion packet, asks "이력서에 뭐 쓸 수 있어", or wants to see what they have been learning
  and where their gaps are.
allowed-tools: Bash, Read, Write, Glob, Grep, AskUserQuestion
---

# Career Review

Read the journal that has already accumulated and turn it into something the user
can hand to another human.

## Step 1 — establish scope

Ask with AskUserQuestion unless the request already says:

- **Period** — this quarter / this year / everything / a custom range
- **Output** — resume bullets · performance-review self-assessment · promotion
  packet · growth retrospective (what I learned and what I still lack)
- **Audience** — external recruiter (jargon-light, outcome-first) vs. internal
  manager (system names and internal context are fine)

## Step 2 — read the journal

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/journal_util.py" stats
```

Then read, in this order:

1. `Career/Achievements.md` — the primary source
2. `Decisions/` in range — evidence of judgment and scope
3. `Projects/` — breadth of systems owned
4. `Learnings/` — trajectory and depth

Filter by the `date` / `last_updated` frontmatter. If the journal is thin, say so
plainly and offer to run `dev-journal` on pending digests first, or
`dev-journal-setup` → `backfill` to pull in history.

## Step 3 — synthesise

Group by **theme, not chronology.** Three to five themes for a year. A theme is
a coherent claim about the user's work — "made the deploy pipeline trustworthy",
"owned the billing subsystem end to end" — not a list of tickets.

Within each theme, pick the two or three strongest items. Depth beats coverage; a
resume with 4 sharp bullets outperforms one with 15 vague ones.

Rank candidate bullets by:

1. **Scope** — how many people or systems were affected
2. **Difficulty** — was there a real fork, a real failure mode, a real constraint
3. **Evidence** — is there a number, a before/after, or a named artifact
4. **Transferability** — will a stranger recognise this as valuable

### Bullet form

`<Action verb> <object> <in what system>, <result>` — mechanism after the result
if it earns its place.

- Lead with the outcome, not the activity.
- Keep the number if the journal has one. If it does not, write the mechanism and
  append `[metric needed]`. **Never invent a metric.** Flag anything the user
  should verify before sending.
- Cut "helped with", "worked on", "was responsible for", "utilized".
- One line each. Two at most.

### For a performance review

Add, beyond the bullets: collaboration and influence (subagent/team work in the
digests), judgment under uncertainty (the Decisions notes), and growth (the
Learnings trajectory). Include one honest gap with what the user is doing about
it — reviewers trust self-assessments that contain a real weakness.

### For a growth retrospective

Answer three questions from evidence, not vibes:

- What did I get measurably better at, and what in the journal proves it?
- What keeps recurring in my errors? (scan `Sessions/` 막혔던 지점 and
  `Learnings/` for repeated root causes — this is the highest-value output)
- What have I never touched that my next role needs?

## Step 4 — deliver

Write the result to `<vault>/DevJournal/Career/<Period>-<Output>.md` with
frontmatter `type: career-review`, and also show the bullets inline in chat — the
user will want to edit them immediately.

Close by listing every `[metric needed]` marker in one place, so the user knows
exactly what to look up before they send it anywhere.

## Honesty constraint

This output goes to recruiters and managers. Every claim must trace to something
in the journal. When the journal supports a weaker claim than the user would
like, write the weaker claim and say why. Overstated bullets fail in interviews,
where the user has to defend them.
