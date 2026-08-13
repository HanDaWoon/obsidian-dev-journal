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

다이제스트에 담기는 것: 내가 입력한 프롬프트 원문, AskUserQuestion으로 고른
선택지, 승인한 계획, 수정한 파일, 실행한 의미 있는 명령, 부딪힌 에러, 세션 중
만든 커밋. 해석은 하지 않는다.

## 설치

이 리포 자체가 플러그인 마켓플레이스다. Claude Code에서:

```
/plugin marketplace add <github-user>/obsidian-dev-journal
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

**한 가지 한계**: worktree를 이미 삭제했다면 git에 어느 레포였는지 물어볼 수 없다.
그런 다이제스트는 `repo_name_guessed: true`와 경고가 붙고 디렉터리 이름을 레포
이름으로 쓴다. 합성 전에 `repo:` 필드를 직접 고치면 된다.

```bash
python3 $S/journal_util.py backfill --dry-run --limit 40   # 먼저 확인
python3 $S/journal_util.py backfill --repo my-api          # 특정 레포만
python3 $S/journal_util.py backfill --since-days 90        # 최근 것만
python3 $S/journal_util.py backfill --limit 0              # 전부
```

## 설정

`~/.claude/obsidian-dev-journal/config.json`

| 키 | 기본값 | 의미 |
| --- | --- | --- |
| `vault_path` | `""` | Obsidian 볼트 절대경로. `OBSIDIAN_VAULT` 환경변수로도 가능 |
| `base_folder` | `DevJournal` | 볼트 안에서 이 플러그인이 쓰는 폴더 |
| `min_user_prompts` | `2` | 이보다 짧은 세션은 건너뛴다 |
| `exclude_paths` | `[]` | 이 경로 아래 작업은 절대 기록하지 않는다 |
| `redact` | `true` | 명령/프롬프트에서 시크릿 패턴 마스킹 |
| `max_prompts` / `max_commands` / `max_digest_chars` | 40 / 40 / 14000 | 다이제스트 크기 상한 |

## 알아둘 것

- **프라이버시**: 다이제스트에는 프롬프트 원문, 파일 경로, 명령줄이 평문으로
  들어간다. 볼트가 외부로 동기화된다면 민감한 레포는 `exclude_paths`에 넣는다.
  `redact`는 `KEY=`, `Bearer`, `sk-…`, DB URL 자격증명 같은 흔한 패턴을 가리지만
  **보장이 아니다**.
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
python3 tests/test_capture.py     # 회귀 스위트 53건, 의존성 없음
```

설계 근거와 깨면 안 되는 제약은 `CLAUDE.md`에 있다. 코드를 고치기 전에 읽는다.

## 스크립트 직접 쓰기

```bash
S=~/.claude/plugins/*/obsidian-dev-journal/scripts   # 설치 위치에 맞게

python3 $S/journal_util.py doctor                    # 상태 점검
python3 $S/journal_util.py init --vault ~/MyVault    # 설정
python3 $S/journal_util.py pending                   # 대기 중인 다이제스트
python3 $S/journal_util.py backfill --limit 20       # 과거 세션 소급
python3 $S/journal_util.py stats                     # 현황

# 볼트를 건드리지 않고 파서 출력만 보기
python3 $S/capture_session.py --transcript <t.jsonl> --force --stdout
```

MIT.
