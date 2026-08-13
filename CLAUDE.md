# obsidian-dev-journal — 개발 컨텍스트

Claude Code 세션을 Obsidian 노트로 자동 기록하는 플러그인. 이 문서는 **왜 이렇게
만들었는지**를 남긴다. 아래 제약들은 대부분 실제 버그를 밟고 나서 생긴 것이라,
근거를 모르고 "정리"하면 그 버그가 그대로 되살아난다.

## 아키텍처 — 캡처와 해석의 분리

```
세션 종료 → SessionEnd 훅(수 ms) → 페이로드만 디스크에 던지고 즉시 반환
         → 분리된 백그라운드 프로세스: transcript(수 MB) → 다이제스트(~1-14KB)
         → <vault>/DevJournal/_raw/*.md  (status: unprocessed)
         → 사용자가 원할 때 dev-journal 스킬이 8개씩 묶어 LLM 1패스로 합성
```

이 분리가 이 플러그인의 존재 이유다. **캡처 경로에서 LLM을 호출하면 안 된다** —
코딩 세션당 추가 토큰 0이라는 게 핵심 가치다. 세션마다 요약을 돌리고 싶은
유혹이 생기면, 그게 바로 이 설계를 버리는 것임을 알고 결정해야 한다.

## 절대 깨면 안 되는 것들

### 1. 훅은 세션 종료를 지연시키지 않는다
`SessionEnd` 훅의 시간 예산은 매우 작다(공유 1.5초 수준). `on_session_end.sh`는
페이로드 저장 + double-fork만 한다. 여기에 파싱이나 git 호출을 추가하면 사용자가
종료 지연을 체감한다. double-fork는 Claude Code가 훅의 process group을 kill해도
캡처가 살아남게 하려는 것이고, `</dev/null`은 자식이 stdin 파이프를 붙잡지 않게
하려는 것이다.

### 2. transcript 포맷은 내부 규격이다
공식 문서가 "내부 규격이며 릴리스마다 바뀔 수 있으니 직접 파싱하지 말라"고
명시한다. 그래도 자동 캡처를 하려면 파싱이 유일한 길이므로, **모든 필드 접근이
방어적이어야 한다.** `_consume_entry`는 항목 단위로 try/except에 감싸져 있다 —
이상한 줄 하나가 다이제스트 전체를 날린 버그가 실제로 있었다. 크래시가 아니라
"덜 자세한 다이제스트 + 상단 경고"로 degrade하는 것이 계약이다.

### 3. 시크릿 마스킹은 선형 시간이어야 한다
`SECRET_KEYWORD_RE`(키워드 탐색) + `ASSIGN_TAIL_RE`(위치 고정 매칭) 2단계 구조를
**하나의 정규식으로 합치지 말 것.** 선행 `.*`를 둔 버전이 12MB transcript 파싱을
58초로 만들었다(현재 0.4초). 마스킹 순서도 의미가 있다: `BEARER_RE`가 먼저 돌아야
`Authorization: Basic <cred>`에서 키워드 단계가 "Basic"을 값으로 삼아먹고 실제
자격증명을 남기는 일이 없다.

마스킹은 **보장이 아니다**. 민감한 레포는 `exclude_paths`로 제외하는 게 정답이고,
README와 셋업 스킬이 그렇게 안내한다.

### 4. 프라이버시 게이트는 cwd 확정 후에 검사한다
파일명 안정화를 위해 "transcript의 cwd 우선" 로직을 넣으면서, 검사 이후에 cwd가
바뀌는 구멍이 생긴 적이 있다. 제외 검사는 페이로드 cwd와 확정된 cwd **양쪽**에서
돈다. 순서를 바꾸지 말 것.

### 5. worktree는 `--git-common-dir`로 귀속시킨다
`--show-toplevel`은 worktree 경로를 돌려주므로, 그걸 쓰면 `feat-x`, `hotfix`가
각각 별개 프로젝트가 되어 지식이 갈라진다. common dir은 항상 원본 레포의 `.git`을
가리킨다. bare 레포(`myrepo.git`)와 상대경로 `.git` 케이스도 처리한다.
diff/log는 worktree(체크아웃)에서, 이름은 원본 레포에서 가져온다.

### 6. PreCompact/SessionEnd 중복은 원자적으로 처리한다
한 세션에 캡처가 두 번 돌 수 있고, 두 프로세스가 동시에 살아있을 수도 있다.
규칙 두 개: 풍부한 다이제스트를 얇은 것으로 바꾸지 않는다, 이미 합성된
다이제스트는 건드리지 않는다. 판정은 **frontmatter(앞 600자)에 앵커링**되어 있다 —
캡처된 프롬프트에 "status: processed"가 들어있어도 dedup을 속일 수 없어야 한다.
쓰기는 tmp + `os.replace`이고, 교체 직전에 한 번 더 확인한다.

### 7. git에 날짜를 넘길 때는 정규화한다
transcript 타임스탬프(`2026-03-10T11:00:00.000Z`)를 git에 그대로 주면 **에러 없이
빈 로그**가 돌아온다. `git_time()`을 거쳐야 한다. 이 버그 때문에 "세션 중 커밋"
섹션이 한동안 조용히 비어 있었다. 그리고 `--since`만 주면 backfill한 옛 세션이
그 이후 모든 커밋을 끌어오므로 `--until`과 항상 함께 쓴다. `git diff`(dirty)는
세션 종료 6시간 이내일 때만 의미가 있다.

## 파일 지도

| 파일 | 역할 |
| --- | --- |
| `scripts/on_session_end.sh` | 훅 진입점. 즉시 반환 + 로그 회전 + 고아 페이로드 청소 |
| `scripts/capture_session.py` | transcript → 다이제스트. LLM 미사용. 성능 민감 |
| `scripts/journal_util.py` | CLI: init/doctor/pending/cat/done/backfill/stats |
| `skills/dev-journal/` | 합성 스킬 + `references/note-templates.md` |
| `skills/dev-journal-setup/` | 볼트 연결, 진단, backfill 안내 |
| `skills/career-review/` | 이력서 불릿·성과 리뷰·성장 회고 |
| `hooks/hooks.json` | SessionEnd + PreCompact. `${CLAUDE_PLUGIN_ROOT}` 사용 |
| `tests/test_capture.py` | 회귀 스위트 53건. 의존성 없음 |

상태는 전부 홈 디렉터리에 있다: `~/.claude/obsidian-dev-journal/`(config.json,
logs/, pending/, staged/). 리포에는 코드만 들어간다.

## 작업 규칙

- **수정 후 반드시** `python3 tests/test_capture.py` — 53건 전부 통과해야 한다.
  테스트 케이스마다 어떤 버그를 붙잡고 있는지 주석이 있다. 근거를 모르고 삭제하지
  말 것.
- 훅/스킬 파일을 고치면 Claude Code에서 `/plugin update` 또는 `/reload-plugins`
  후 재시작해야 반영된다(SKILL.md 본문은 즉시 반영).
- 스킬 SKILL.md는 3000단어 미만으로 유지한다. 상세 내용은 `references/`로 뺀다.
- 다이제스트에 새 정보를 추가할 때는 반드시 `redact()`를 통과시킨다. 프롬프트,
  명령어, AskUserQuestion 답변, 서브에이전트 설명, 검색 쿼리가 이미 그렇게 되어
  있다 — 새 필드를 추가하면서 빠뜨린 적이 있다.
- 볼트에 쓰는 노트의 frontmatter 키를 바꾸면 `references/note-templates.md`의
  Dataview 쿼리도 같이 고쳐야 한다.

## 합성 품질에 대한 입장

`Decisions/`와 `Learnings/`는 아무거나 기록하지 않는다. 살아있는 대안이 있었던
갈림길, 다음 달에도 참인 사실만 남긴다. 이 기준이 없으면 볼트가 쓰레기장이 되고,
그러면 아무도 안 본다. 얇은 세션에서 노트 0개가 나오는 것은 **정상**이다.

성과 불릿은 이력서로 간다. 숫자를 만들어내지 말고, 없으면 메커니즘과 범위를 쓰고
`[metric needed]`를 붙인다.

## 알려진 한계

- 마스킹은 흔한 패턴만 잡는다(`KEY=`, `--flag value`, `-u user:pw`, JSON 키,
  Bearer/Basic, DB URL, `sk-`/`ghp_`/JWT 형태). 임의의 시크릿은 못 잡는다.
- 삭제된 worktree는 어느 레포였는지 복구할 수 없다 → `repo_name_guessed: true`.
- backfill은 `~/.claude/projects/`에 transcript가 남아있는 만큼만 가능하다.
