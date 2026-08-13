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
[[2026-08-11-rate-limit-redis-vs-inmemory]]
```

---

## 4. `Projects/<repo>.md`

A living map of the codebase. Read, then revise — this note should get denser,
not longer. Prune lines that are no longer true.

```markdown
---
type: project
repo: my-api
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
- 테스트는 `REDIS_URL` 이 없으면 조용히 통과한다 — CI 에서만 실제로 돈다.

## 자주 쓰는 명령
- `make test-int` — 통합 테스트 (docker 필요)

## 결정 이력
- 2026-08-11 [[2026-08-11-rate-limit-redis-vs-inmemory]]
```

---

## 5. `Career/Achievements.md`

Append-only. One `##` heading per month, newest month at the top.

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

## Optional index note

Offer to create `<vault>/DevJournal/DevJournal.md` once. Requires the Dataview
plugin.

```markdown
# Dev Journal

## 최근 세션
```dataview
TABLE repo, duration, date
FROM #dev-journal AND "DevJournal/Sessions"
SORT date DESC
LIMIT 20
```

## 결정 로그
```dataview
TABLE repo, status, date
FROM #decision
SORT date DESC
```

## 처리 대기 중인 다이제스트
```dataview
LIST
FROM "DevJournal/_raw"
WHERE status = "unprocessed"
```

## 흔들리는 지식 (복습 필요)
```dataview
LIST
FROM #learning
WHERE confidence = "shaky"
SORT last_updated ASC
```
```
