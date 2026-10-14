# Obsidian Dev Journal

Claude Code 세션을 자동으로 Obsidian 노트로 남긴다. 내가 뭘 **결정했는지**, 뭘
**배웠는지**, **이력서에 쓸 수 있는 성과**가 무엇인지가 코딩만 해도 쌓인다.

만든 이유: Claude Code에 맡기다 보면 결정과 학습이 대화 로그 속에서 사라진다.
6개월 뒤 "그때 왜 그렇게 했지", "올해 뭐 했지"에 답할 근거가 없다.

## 토큰을 거의 쓰지 않는 구조

핵심은 **캡처와 해석을 분리**한 것이다.

```
세션 종료
   ↓  SessionEnd 훅 (수 ms, 즉시 반환)
페이로드를 디스크에 던지고 백그라운드 프로세스 분리
   ↓  capture_session.py — LLM 호출 없음, 토큰 0
transcript 수 MB → 사실만 남긴 1~14KB 다이제스트
   ↓  <vault>/DevJournal/_raw/*.md  (status: unprocessed)
내가 원할 때 /dev-journal
   ↓  다이제스트 여러 개를 묶어 한 번에 합성 (LLM 1패스)
Sessions · Decisions · Learnings · Projects · Career 노트
```

세션마다 요약 LLM을 돌리는 방식과 비교해 코딩 중 추가 비용이 0이고, 합성은
8개 세션당 1회로 묶인다. 12MB transcript 파싱에 0.3초.

다이제스트에 담기는 것: 내가 입력한 프롬프트 원문, 세션 recap(어시스턴트가 만든
요약 — 출처를 구분해 별도 섹션에 담는다), AskUserQuestion으로 고른 선택지, 승인한
계획, 수정한 파일, 실행한 의미 있는 명령, 부딪힌 에러, 세션 중 만든 커밋.
해석은 하지 않는다.

프롬프트는 사용자가 실제로 한 말만 남는다. 큐에 들어갔다 전달되며 두 번 기록되는
입력, `<task-notification>` 같은 harness 주입 텍스트는 걸러진다(실측 기준 캡처량의
절반).

에러 섹션에는 훅이 실패로 표시한 것뿐 아니라 **출력이 실패로 보이는 명령**도 들어간다
(traceback, `N failed`, `command not found` 등). 후자는 `_(inferred from output)_`
로 표시되므로 읽는 쪽에서 가려낼 수 있다.

다이제스트 상태는 셋이다: `unprocessed` → `processed`, 그리고 의도적으로 미룬
`held`(`hold --reason`). 보류한 것은 `pending`에서 빠지되 따로 집계된다.
합성이 끝난 다이제스트는 `prune`으로 볼트 밖으로 물릴 수 있다 — `_raw/`는 큐지
보관소가 아니다.

## 설치

이 리포 자체가 플러그인 마켓플레이스다. Claude Code에서:

```
/plugin marketplace add HanDaWoon/obsidian-dev-journal
/plugin install obsidian-dev-journal@dev-journal
```

스코프를 물으면 **User scope**를 선택해야 모든 프로젝트에 적용된다.
로컬 클론에서 설치하려면 `/plugin marketplace add ~/path/to/repo`.

1. Claude Code를 재시작한다 (훅은 재시작 또는 `/reload-plugins` 후 적용된다).
2. `dev-journal-setup` 스킬을 실행한다 — 볼트를 찾아주고 폴더를 만들고 훅이
   동작하는지 검증한다. 셋업은 홈 디렉터리 기준이라 **한 번만** 하면 된다.
3. 원하면 지난 세션을 소급 적용한다 (`backfill`). `~/.claude/projects/` 에
   transcript가 남아 있어서 설치 이전 기록도 살릴 수 있다.

업데이트는 리포에 push 후 `/plugin update obsidian-dev-journal`.

## 스킬

| 스킬 | 언제 |
| --- | --- |
| `dev-journal-setup` | 볼트 연결, 진단, 과거 세션 백필 |
| `dev-journal` | 쌓인 다이제스트를 노트로 합성. "저널 정리해줘" |
| `career-review` | 이력서 불릿, 성과 리뷰 자기평가, 성장 회고 |

## 볼트 구조

```
<vault>/DevJournal/
├── _raw/          기계 다이제스트 (입력, 손대지 않는다)
├── Sessions/      세션별 기록 — 모든 링크의 기준점
├── Decisions/     실제 갈림길만. ADR 경량판
├── Learnings/     주제별 1노트, 계속 덧붙인다
├── Projects/      레포별 살아있는 지식 맵
└── Career/        Achievements.md — append-only 성과 원재료
```

`Learnings`는 서로 링크한다. 주제 노트를 새로 쓰거나 덧붙일 때 관련된 다른
`Learnings/` 노트 2~3개를 `## 관련`에 걸고 저쪽에도 역방향으로 건다. 이게 없으면
주제 노트가 세션에서 들어오기만 하는 잎이 되어 서로 고립된다(실측 41개 중 17개).
Sessions는 로그고, **1년 뒤에도 쓸모 있는 건 Learnings 그래프**인데 그 그래프는
이 링크가 있어야 존재한다. 어떤 노트가 있는지는 `journal_util.py topics --query
<주제>` 로 본다 — 이름·`## 핵심` 한 줄·태그·이미 걸린 이웃을 돌려주므로 수십 개를
다 읽지 않아도 된다.

`Decisions`는 아무 선택이나 기록하지 않는다. **살아있는 대안이 있었고 다른
코드베이스로 이어졌을 갈림길**만 남긴다. 그 기준이 없으면 볼트가 쓰레기장이
된다. `Learnings`도 같다 — 다음 달에도 참인 것만.

## Git worktree

Claude Code는 **작업 디렉터리 기준**으로 transcript를 보관하고, worktree는 별개의
작업 디렉터리다. 그래서 worktree마다 `~/.claude/projects/` 밑에 자기 폴더가 생기고,
`backfill`은 그 전체를 스캔하므로 **worktree 세션도 자동으로 포함된다.**

레포 귀속은 `git rev-parse --git-common-dir`로 판단한다. `--show-toplevel`은
worktree 경로를 돌려주기 때문에 그대로 쓰면 `feat-x`, `hotfix`가 각각 별개
프로젝트로 갈라진다. common dir은 항상 원본 레포의 `.git`을 가리키므로:

| 세션 위치 | `repo` | `worktree` |
| --- | --- | --- |
| `~/src/my-api` | `my-api` | – |
| `~/src/feat-x` (형제 worktree) | `my-api` | `feat-x` |
| `~/src/my-api/.worktrees/hotfix` | `my-api` | `hotfix` |

한 레포의 모든 worktree가 `Projects/my-api.md` 하나로 모인다. worktree 이름은
세션 노트에만 남는다(병렬 작업의 맥락이라서).

세션이 레포의 **하위 디렉터리**에서 시작해도(`my-api/tests`) 정본 레포로 귀속된다.
그리고 세션의 레포는 **가장 오래 머문 디렉터리** 기준이다 — 마지막에 `cd` 한 곳이
아니다.

**한 가지 한계**: worktree를 이미 삭제했다면 git에 어느 레포였는지 물어볼 수 없다.
그런 다이제스트는 `repo_name_guessed: true`와 경고가 붙고 디렉터리 이름을 레포
이름으로 쓴다. 합성 전에 `repo:` 필드를 직접 고치면 된다.

```bash
python3 $S/journal_util.py backfill --dry-run --limit 40   # 먼저 확인
python3 $S/journal_util.py backfill --repo my-api          # 특정 레포만
python3 $S/journal_util.py backfill --since-days 90        # 최근 것만
python3 $S/journal_util.py backfill --limit 0              # 전부
```

## 여러 볼트를 나중에 하나로 합치기

머신마다(또는 회사/개인으로) 볼트를 따로 쓰다가 나중에 합치는 건 된다. 다이제스트는
자기완결적이고, 중복 판정이 파일명이 아니라 frontmatter의 `session_id`(UUID)라
`_raw/`끼리 그냥 옮겨 붙여도 어긋나지 않는다.

합치기 전에 알아둘 것:

- **머신 이름은 소급이 안 된다.** `host:` 필드는 캡처 시점에만 알 수 있다. 합칠
  가능성이 있으면 지금 머신마다 `host_name`을 지정해 두는 게 낫다. 이미 캡처된
  다이제스트는 `host: ""`로 남는다(`journal_util.py stats`가 호스트별로 세어준다).
- **레포 이름이 겹칠 수 있다.** 레포 식별은 `--git-common-dir`의 basename이라,
  양쪽 볼트의 서로 다른 `api`가 하나의 `Projects/api.md`로 합쳐진다. 합치기 전에
  한쪽을 리네임한다.
- **이름이 곧 키인 노트**는 수동 병합이 필요하다: `Learnings/<주제>.md`,
  `Projects/<repo>.md`. 둘 다 날짜 섹션을 덧붙이는 구조라 본문 합치기는 기계적이다.
  `Sessions/`와 `Decisions/`는 날짜+슬러그라 충돌이 드물다.
- 합친 뒤 `journal_util.py index --force`로 인덱스 노트를 다시 쓴다. Dataview
  쿼리에 `base_folder` 경로가 박혀 있어서, 볼트마다 폴더 이름이 달랐다면 필요하다.

순서: `_raw/` 이동 → `pending`으로 미합성분 확인 → `/dev-journal` → 동명 노트 병합
→ `index --force`.

## 설정

`~/.claude/obsidian-dev-journal/config.json`

| 키 | 기본값 | 의미 |
| --- | --- | --- |
| `vault_path` | `""` | Obsidian 볼트 절대경로. `OBSIDIAN_VAULT` 환경변수로도 가능 |
| `base_folder` | `DevJournal` | 볼트 안에서 이 플러그인이 쓰는 폴더 |
| `host_name` | `""` | 다이제스트에 남길 머신 이름. 비우면 호스트네임의 첫 라벨(`mbp.local` → `mbp`) |
| `min_user_prompts` | `2` | 이보다 짧은 세션은 건너뛴다 |
| `min_files_touched` | `0` | 프롬프트가 적어도 이만큼 파일을 고쳤으면 기록한다. **하한이 아니라 추가 통과 조건**이고, 최소 1로 바닥이 잡힌다 |
| `exclude_paths` | `[]` | 이 경로 아래 작업은 절대 기록하지 않는다. cwd 부분 문자열 매칭, `~`·`$VAR` 확장됨 |
| `redact` | `true` | 명령/프롬프트에서 시크릿 패턴 마스킹 |
| `extra_commands` | `""` | 기록할 명령 목록을 넓히는 정규식. 도메인 도구용 (`\b(daml\|sui\|vast)\b`) |
| `max_prompts` / `max_commands` / `max_digest_chars` | 40 / 40 / 14000 | 다이제스트 크기 상한 |
| `max_recaps` / `max_recap_chars` | 12 / 400 | 세션 recap 섹션 상한 |
| `git_autocommit` | `false` | `done` 이 끝날 때 저널 폴더를 커밋한다. 폴더(또는 볼트)가 git 레포여야 한다. 커밋은 저널 폴더로만 한정된다 |
| `git_push` | `true` | `git_autocommit` 이 켜져 있고 브랜치에 upstream 이 있으면 커밋 뒤 푸시한다. 푸시 실패는 `done` 을 실패시키지 않고 출력의 `git.push_error` 로 보고된다 |

`git_autocommit` 을 쓸 때는 저널 폴더의 `.gitignore` 에 `_raw/` 를 넣는 것을 권한다 —
다이제스트는 큐라서 `prune` 으로 볼트를 떠나고, 명령줄과 에러 출력이 거의 그대로 들어 있다.
`doctor` 의 `journal_git` 이 어느 레포·브랜치·upstream 으로 커밋되는지 보여준다.

상한에 걸리면 꼬리를 자르지 않고 **섹션별로 깎는다** — 값싼 것부터(외부 조회 →
서브에이전트 → 명령 → 프롬프트 → recap → 계획 → 파일 → 에러 → 질문). 커밋 목록은
깎지 않는다.

## 세션이 쌓이면

측정치(디지스트 5000개, 30MB 기준):

| 동작 | 시간 |
| --- | --- |
| 세션 종료 캡처 (5000개 디렉터리에 쓰기) | 0.24s |
| `pending` / `stats` / `doctor` / `prune` | 각 0.3~0.4s |
| 10MB transcript 파싱 | 0.5s |

스크립트 쪽은 선형이고 상수가 작아서 문제되지 않는다. 실제 병목은 두 군데였다:

- **`pending` 출력 크기.** 미처리 1667건이면 JSON 634KB(≈16만 토큰)라 스킬
  1단계에서 컨텍스트가 터진다. 이제 기본 40건만 돌려주고 `count` 로 총계를,
  `truncated` 로 잘렸음을 알린다. 전량이 필요하면 `--limit 0`.
- **Obsidian 자체.** `_raw/` 는 다이제스트당 ~6KB 이고 Obsidian 이 전부 검색·
  그래프·Dataview 에 색인한다. 이게 `prune` 이 있는 이유다 — 합성이 끝난 것은
  볼트 밖 archive 로 (되돌릴 수 있게) 옮긴다. 인덱스 노트의 쿼리에도 전부
  `LIMIT` 이 걸려 있다.
- `Career/Achievements.md` 는 append-only 라 상한이 없다. 100불릿쯤에서
  `Achievements-<연도>.md` 로 분리한다(`career-review` 는 `Career/` 폴더 전체를 읽는다).

## 알아둘 것

- **프라이버시**: 다이제스트에는 프롬프트 원문, 파일 경로, 명령줄이 평문으로
  들어간다. 볼트가 외부로 동기화된다면 민감한 레포는 `exclude_paths`에 넣는다.
  `~/work/private` 처럼 `~` 를 써도 되고, `doctor` 가 각 항목이 실제로 무엇으로
  풀리는지 보여준다. `redact`는 `KEY=`, `Bearer`, `sk-…`, DB URL 자격증명 같은
  흔한 패턴을 가리지만 **보장이 아니다**.
- **볼트에 못 쓰면 잃지 않고 staging 한다.** 외장 드라이브가 빠졌거나 동기화
  클라이언트가 폴더를 잠갔을 때 다이제스트는 `~/.claude/obsidian-dev-journal/staged/`
  로 간다. `init` 을 다시 돌리면 볼트로 옮겨온다.
- **transcript 포맷은 내부 규격**이다. Claude Code 업데이트로 바뀔 수 있다.
  파서는 모든 필드를 방어적으로 읽어서, 포맷이 바뀌면 크래시가 아니라 "덜 자세한
  다이제스트"로 degrade하고 상단에 경고를 붙인다. 경고가 보이면
  `dev-journal-setup` 진단을 돌린다.
- **훅은 세션 종료를 지연시키지 않는다.** SessionEnd 훅의 시간 예산이 매우 작기
  때문에 래퍼는 페이로드만 저장하고 즉시 빠진다. 실제 파싱은 분리된 백그라운드
  프로세스에서 돈다.
- 캡처가 안 되는 것 같으면 `~/.claude/obsidian-dev-journal/logs/capture.log`.

## 개발

```bash
python3 tests/test_capture.py     # 회귀 스위트 259건, 의존성 없음
```

설계 근거와 깨면 안 되는 제약은 `CLAUDE.md`에 있다. 코드를 고치기 전에 읽는다.

## 스크립트 직접 쓰기

```bash
S=~/.claude/plugins/*/obsidian-dev-journal/scripts   # 설치 위치에 맞게

python3 $S/journal_util.py doctor                    # 상태 점검
python3 $S/journal_util.py init --vault ~/MyVault    # 설정
python3 $S/journal_util.py pending                   # 대기 중인 다이제스트 (+ 배치 제안)
python3 $S/journal_util.py hold <f> --reason "..."   # 의도적으로 미루기
python3 $S/journal_util.py unhold <f>                # 다시 큐로
python3 $S/journal_util.py index                     # 볼트 인덱스 노트 생성
python3 $S/journal_util.py prune --older-than 90 --dry-run   # 합성 끝난 다이제스트 정리
python3 $S/journal_util.py backfill --limit 20       # 과거 세션 소급
python3 $S/journal_util.py topics --query canton     # 주제 노트 지도 (링크용)
python3 $S/journal_util.py stats                     # 현황

# 볼트를 건드리지 않고 파서 출력만 보기
python3 $S/capture_session.py --transcript <t.jsonl> --force --stdout
```

MIT.
