# Note templates

Use these shapes verbatim. Frontmatter keys must match exactly — the Dataview
queries at the bottom depend on them. Write body text in the user's language.

---

## 1. `Sessions/YYYY-MM-DD-<repo>-<slug>.md`

The anchor note. One per processed digest, always written.

```markdown
---
type: session
date: 2026-08-11
repo: my-api          # 항상 정본 레포. worktree 이름을 넣지 않는다
worktree: feat-x      # worktree에서 돈 세션만. 아니면 이 줄을 뺀다
branch: feat/rate-limit
host: work-mbp        # 다이제스트의 host: 를 그대로. 비어 있으면 이 줄을 뺀다
duration: 1h 20m
tags: [dev-journal, session, my-api]
session_id: "a1b2c3d4"
---

# 2026-08-11 · my-api · 레이트 리미터 도입

## 무엇을 했나
2~4문장. 목표 → 실제로 한 일 → 끝난 상태. 다이제스트의 프롬프트와 커밋에서만 근거를 가져온다.

## 결정
- [[2026-08-11-rate-limit-redis-vs-inmemory]]

## 배운 것
- [[Redis 원자성과 Lua 스크립팅]]

## 막혔던 지점
- `tests/test_limiter.py` 에서 `KeyError: 'window'` — 만료된 키를 읽는 경로. 원인/해결은 학습 노트 참조.

## 남은 일
- [ ] 429 응답에 `Retry-After` 헤더 추가
- [ ] 부하 테스트

## 흔적
- 파일 7개 수정 · 커밋 3개 · 에러 4회
- `_raw/2026-08-11--my-api--a1b2c3d4.md`
```

If a section has no content, write `- 없음` rather than deleting the heading —
consistent structure keeps the notes skimmable.

`host:` must be carried over from the digest, not re-derived. Digests are
archived out of the vault once processed (`journal_util.py prune`), so the
session note is the only place that still knows which machine a session ran on
— the thing you need if two vaults are ever merged into one.

---

## 2. `Decisions/YYYY-MM-DD-<slug>.md`

ADR-lite. Only for real forks (see the bar in SKILL.md).

```markdown
---
type: decision
date: 2026-08-11
repo: my-api
status: decided        # decided | superseded | revisited
tags: [dev-journal, decision, my-api]
---

# Redis 카운터 vs 인메모리 카운터

## 맥락
왜 이 선택이 필요했나. 1~3문장.

## 선택
Redis + Lua 스크립트로 원자적 카운터 구현.

## 대안
| 대안 | 장점 | 버린 이유 |
| --- | --- | --- |
| 인메모리 dict | 의존성 0, 가장 빠름 | 인스턴스 3대라 카운터가 갈라짐 |
| DB 테이블 | 이미 있음 | 쓰기 부하가 요청마다 발생 |

## 근거
다이제스트에 기록된 이유만 적는다. 기록이 없으면 `미기록`.

## 되돌리는 비용
낮음 / 중간 / 높음 — 그리고 왜.

## 출처
[[2026-08-11-my-api-레이트-리미터-도입]]
```

---

## 3. `Learnings/<Topic Name>.md`

One note per topic, appended over time. **Read before writing.** If the topic
note exists, add a new `## 2026-08-11` section; if a previous line is now wrong,
edit it and note the correction.

```markdown
---
type: learning
aliases: [Redis Lua, EVAL]     # 이 주제를 부르는 다른 이름. [[Redis Lua]] 도 이 노트로 온다
tags: [dev-journal, learning, redis]
first_learned: 2026-08-11
last_updated: 2026-08-11
confidence: solid      # shaky | solid
---

# Redis 원자성과 Lua 스크립팅

## 핵심
한 문장 요약. 나중에 이 줄만 읽어도 쓸 수 있어야 한다.

## 2026-08-11 — INCR + EXPIRE 는 원자적이 아니다
- **삽질**: `INCR` 후 `EXPIRE` 를 따로 호출했더니 두 명령 사이에서 죽으면 TTL 없는 키가 남았다.
- **원인**: 파이프라인은 원자성을 보장하지 않는다. MULTI/EXEC 또는 Lua 스크립트가 필요.
- **해결**: `EVAL` 로 두 연산을 한 스크립트에 묶음. (`src/limiter/redis_store.py`)
- **다음에**: TTL 이 필요한 카운터는 처음부터 Lua 로 쓴다.

## 관련
- [[Redis 커넥션 풀 고갈]] — 같은 서브시스템, 이 노트의 원인이 저쪽에서 재발
- [[2026-08-11-rate-limit-redis-vs-inmemory]] · [[my-api]]
```

`## 관련` 은 세션·프로젝트만 나열하는 자리가 아니다. **다른 `Learnings/` 노트로
2~3개 링크**하고, 저쪽 노트에도 역방향 링크를 넣는다. 이게 없으면 주제 노트가
세션에서 들어오기만 하는 잎이 되어 서로 고립된다(실측 41%). 관계를 몇 단어로
같이 적는다 — 같은 서브시스템 / 같은 근본 원인 / 선행 조건 / 이쪽이 저쪽을 대체.
관계가 없으면 링크하지 않는다.

어떤 노트가 있는지는 `journal_util.py topics --query <주제>` 로 본다 — 이름,
`## 핵심` 한 줄, 태그, 이미 걸린 이웃까지 돌려주므로 41개를 다 읽지 않아도 된다.

---

## 4. `Projects/<repo>.md`

A living map of the codebase. Read, then revise — this note should get denser,
not longer. Prune lines that are no longer true.

```markdown
---
type: project
repo: my-api
aliases: [my-api, MyAPI]       # 레포를 부르는 다른 이름. worktree 이름은 넣지 않는다
tags: [dev-journal, project, my-api]
last_updated: 2026-08-11
sessions: 12
---

# my-api

## 한 줄 정의
무엇을 하는 서비스인가.

## 구조
- `src/api/` — FastAPI 라우터, 얇게 유지. 비즈니스 로직 금지.
- `src/limiter/` — 레이트 리미팅. Redis 백엔드.

## 불변 규칙
- 모든 외부 호출은 `clients/` 를 거친다. 라우터에서 직접 `httpx` 금지.
- 마이그레이션은 alembic, 수동 DDL 금지.

## 함정

> [!warning] 조용히 실패하는 것들
> 테스트는 `REDIS_URL` 이 없으면 조용히 통과한다 — CI 에서만 실제로 돈다.

Obsidian callout(`> [!warning]`, `> [!tip]`, `> [!danger]`)은 접히고 색이 붙어서
훑을 때 눈에 띈다. 함정·불변 규칙처럼 "나중에 놓치면 아픈 것"에만 쓴다. 남용하면
아무것도 안 쓴 것과 같아진다.

## 자주 쓰는 명령
- `make test-int` — 통합 테스트 (docker 필요)

## 결정 이력
- 2026-08-11 [[2026-08-11-rate-limit-redis-vs-inmemory]]
```

---

## 5. `Career/Achievements.md`

Append-only. One `##` heading per month, newest month at the top.

**연 단위로 갈아탄다.** append-only 는 상한이 없다 — 몇 년 쌓이면 한 파일이
수백 KB 가 되고, `career-review` 가 매번 그걸 통째로 읽는다. 파일이 대략 100개
불릿(≈50KB)을 넘으면 지난 해 분량을 `Career/Achievements-2025.md` 로 잘라내고
현재 파일 상단에 `> [!info] 이전 기록 [[Achievements-2025]]` 를 남긴다.
`career-review` 는 `Career/` 폴더 전체를 읽으므로 범위는 그대로다.

```markdown
---
type: career
tags: [dev-journal, career, resume]
last_updated: 2026-08-11
---

# 성과 기록

> 이력서·성과 리뷰용 원재료. 숫자가 없는 항목은 `[metric needed]` 로 표시해두고 나중에 채운다.

## 2026-08

- **분산 환경에서 동작하는 레이트 리미터를 설계·구현**해 인스턴스 3대 간 카운터 불일치 문제를 제거했다. Redis Lua 스크립트로 카운터 갱신을 원자화. `my-api` · [[2026-08-11-my-api-레이트-리미터-도입]]
- **통합 테스트가 Redis 없이 조용히 통과하던 결함을 발견·수정**해 CI 가 실제로 검증하도록 만들었다. [metric needed: 이후 잡힌 버그 수]

## 2026-07

- ...
```

Each bullet: bold the action + object, then the result, then the mechanism, then
the repo and a wikilink to its session. Keep it to one or two sentences.

---

## Index note

Do not hand-write it — `journal_util.py index` generates
`<vault>/DevJournal/DevJournal.md` with the Dataview queries already matching
the frontmatter keys above, plus a plugin-free fallback section. Run it once per
vault; `--force` rewrites it after a key or `base_folder` change.

The previous version of this file inlined the note as a markdown block with
Dataview fences nested inside it, which does not render and drifted out of sync
with the generator. If a frontmatter key changes here, change
`INDEX_NOTE` in `scripts/journal_util.py` in the same commit.
