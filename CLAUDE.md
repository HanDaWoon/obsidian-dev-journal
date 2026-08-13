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

### 8. 프롬프트는 채널이 3개다 — 하나로 뭉뚱그리지 말 것
transcript에서 텍스트를 가진 non-assistant 엔트리를 전부 "사용자 프롬프트"로 받으면
**절반이 노이즈가 된다.** 로컬 transcript 136개 실측: 2152개 중 실질 프롬프트는 1049개.

| `type` | 정체 | 처리 |
| --- | --- | --- |
| `user` | 진짜 사용자 턴 | 그대로. **중복도 그대로** — "다시"를 두 번 말한 건 두 턴이다 |
| `queue-answer`처럼 보이는 `queue-operation` | Claude가 바쁠 때 큐에 들어간 입력 | **그림자**. 큐에 넣을 때 + 전달될 때 두 번 기록된다 |
| `system` | harness recap ("Goal was X. Next: Y.") | 프롬프트 아님. **별도 섹션**으로 |

`queue-operation`을 타입만 보고 통째로 버리면 **안 된다** — 실측상 94건은 그 채널에만
존재하는 진짜 사용자 입력이었다(컨테이너 마이그레이션을 촉발한 Bach의 제보가 그 예다).
`dedupe_prompts()`가 "같은 텍스트가 user 채널에도 있으면 그림자를 버린다"로 처리한다.

recap도 **버리면 안 된다.** 세션당 가장 밀도 높은 한 줄이라 합성할 때 「무엇을 했나」와
「남은 일」이 사실상 여기서 나온다. 다만 사용자가 한 말이 아니므로 프롬프트로 섞으면
어시스턴트의 요약을 사용자 의도로 오인해 노트를 쓰게 된다. 그래서 별도 heading이다.

`<task-notification>`과 orchestration 알림은 harness 주입 텍스트라
`AUTO_PROMPT_RE`로 거른다(`<command-name>` 계열과 같은 부류).

부수 효과: 그림자가 사라지면서 1턴짜리 커밋 메시지 생성 호출이 `min_user_prompts`를
더 이상 통과하지 못한다 — 내용 없는 다이제스트 10건이 자동으로 걸러졌다.

### 9. 크기 상한은 꼬리를 자르는 게 아니라 섹션에 배분한다
예전엔 전부 렌더링한 뒤 `max_digest_chars`에서 통째로 잘랐다. 백필 130건 실측 결과
18건이 상한에 걸렸고 **잘린 건 항상 뒤쪽 섹션**이었다 — 커밋 18/18, 에러 16/18 유실.
정작 600자짜리 프롬프트 40개는 멀쩡히 남았다. 가장 밀도 높은 사실만 골라 버린 셈이다.

`fit_sections()`가 `TRIM_PLAN` 순서대로 깎는다: research → subagents → commands →
prompts → recaps → plans → files → (최후) errors → questions.
커밋과 dirty는 절대 안 깎는다(짧고, 무엇이 나갔는지의 유일한 기록이다).

주의할 점 세 가지:
- **크기를 증분으로 추적하면 안 된다.** 섹션이 첫 항목을 잃을 때 "…N more" 마커가
  새로 생기므로 `freed = len(item)`만 빼면 실제보다 작게 계산되고, 루프가 일찍 멈춰
  **결국 꼬리가 잘린다.** 이 드리프트 때문에 fitter를 넣고도 한동안 효과가 없었다.
  매 회차 `total()`을 다시 계산한다(항목 수가 상한이라 비용은 무시할 수준).
- **프롬프트는 가운데부터 버린다**(`keep_edges`). 앞은 세션의 목적, 뒤는 결말이고
  중간이 가장 중복된다.
- **질문은 Q/offered/chose를 한 항목으로 묶는다.** 줄 단위로 깎으면 답 없는 질문이
  남는다. 비워진 섹션도 제목과 마커는 남긴다 — "조회 10건 중 0건 표시"와 "조회 없음"은
  다른 정보다.

floor 합계보다 작은 예산은 원리적으로 못 맞춘다. 그때만 기존 하드 컷이 백스톱으로 돈다.

### 10. 중복 판정은 파일명이 아니라 session_id로 한다
파일명은 `{날짜}--{repo}--{sid8}.md`인데, 세션 도중 체크아웃 이름이 바뀌면
(`zot` → `internal-registry`) repo가 달라져 **같은 세션이 두 파일이 된다.** 이름 기준
중복 검사는 그 쌍둥이를 영원히 못 본다 — 실제로 130건 중 1건이 두 번 합성됐다.
`twins()`가 frontmatter의 `session_id`로 찾아 처리하고, 더 풍부한 쪽을 남긴 뒤
얇은 쌍둥이를 지운다.

### 11. 상태는 셋이다 — unprocessed / held / processed
`held`가 없으면 "미룬 것"과 "밀린 것"이 구분되지 않아 `pending`이 0이 되지 않는다.
`hold --reason`으로 사유를 남기고, `pending`은 held를 별도로 집계한다.
**`done`은 held를 처리하지 않는다** — 거부하고 `unhold`를 안내한다. 보류는 사용자의
의사표시이므로 자동화가 조용히 덮어쓰면 안 된다.

`done --note`는 다이제스트에 어떤 노트가 나왔는지 역참조를 남긴다. 합성은 1:1이 아니다
(워커 15개 = 세션 1개가 정직한 경우가 있다). 이 키가 없으면 "이 노트 어디서 왔나"의
답이 모델이 본문에 적어둔 산문뿐이다.

### 12. repo 추론 실패 사유를 정확히 쓴다
예전엔 항상 "worktree removed?"라고 찍었다. 레포였던 적이 없는 평범한 상위 디렉터리
(`~/Work/GPU-cluster`)에도 그렇게 나와서, 읽는 사람이 있지도 않은 워크트리를 찾게 만들었다.
`repo_identity`가 `reason`을 함께 돌려준다: 디렉터리 자체가 없는 경우와 git 레포가
아닌 경우를 구분한다.

### 13. `--git-common-dir`의 상대경로는 **probe 기준**으로 푼다
git 은 `--git-common-dir` 을 `-C` 로 준 디렉터리 기준 상대경로로 돌려준다
(`~/Work/repo/tests` 에서 물으면 `../.git`). 이걸 toplevel 에 붙이면 `..` 가
레포 **밖으로** 나가서 `~/Work/.git` → repo `Work`, worktree `repo` 가 되고,
`repo_name_guessed: false` 라 경고조차 안 붙는다. 실측: 218개 중 15개 세션이
하위 디렉터리에서 시작했고 볼트 133개 중 11개가 그렇게 잘못 적혔다(2개는 무경고).
`Path(probe) / common` 이어야 한다. worktree 는 절대경로로 오므로 규칙 5는 그대로다.

삭제된 체크아웃을 찾아 위로 올라가는 루프도 **4단계로 제한**한다. 무제한이면
`~` 까지 올라가는데, dotfiles 로 관리되는 홈은 git 레포라서 삭제된 모든 worktree 가
사용자의 홈을 자기 레포라고 주장하게 된다.

### 14. 세션의 cwd 는 "가장 오래 머문 곳"이다
예전엔 엔트리마다 `facts["cwd"]` 를 덮어써서 **마지막** cwd 가 이겼다. 그런데 날짜는
`first_ts` 에서 오고, `journal_util.transcript_cwd()` 는 **첫** cwd 를 읽었다 —
같은 세션을 세 곳이 다르게 봤다. 218개 중 16개가 first≠last 다.
`resolve_cwd()` 가 최빈값(동률이면 이른 쪽)을 고르고, `transcript_cwd()` 도 같은
규칙을 쓴다. 한쪽만 고치면 다시 어긋난다.

최빈값만으로는 부족해서 `refine_identity()` 가 뒤를 받친다. **추론된 이름일 때만**
동작한다(정상 해석된 레포는 절대 안 건드린다):
- 최빈 디렉터리가 레포를 담고만 있는 평범한 폴더일 수 있다(`~/Work/GPU-cluster` 에
  머물며 실제 작업은 `.../ansible_boundless` 에서 한 세션). 세션이 **실제로 방문한**
  디렉터리 중 git 이 해석되는 것을 방문 횟수 순으로 고른다.
- 그래도 안 되면 방문한 조상 중 가장 얕은 것으로 물러난다 — `<project>/scripts` 가
  repo `scripts` 로 적히는 것보다 낫다.
후보를 "실제 방문한 디렉터리"로 한정하는 게 핵심이다. 무작정 위로 올라가면 13번의
홈 디렉터리 문제가 그대로 재현된다.

**주의**: 이미 만들어진 다이제스트의 repo 를 나중에 재계산해서 덮어쓰면 안 된다.
캡처 시점에 존재하던 worktree 가 지금은 삭제됐을 수 있어서, "고치는" 재계산이
`shinhanx~v1-allocation-factory` 를 `v1-allocation-factory`(guessed)로 **퇴화**시킨다.
실제로 이 실수를 한 번 했고, 다이제스트 본문의 heading(`# Raw session digest — repo
(worktree \`wt\`)`)이 frontmatter 와 독립적이라 그걸로 복구했다. 과거 기록은 캡처
당시의 사실이다.

### 15. 셸 실패는 플래그가 아니라 출력으로 알아낸다
실패한 bash 는 대개 `is_error` 없이 평범한 tool_result 로 온다. 실측: 잡힌 에러
345건, 놓친 셸 실패 243건. 50턴 넘는데 `errors_hit: 0` 인 다이제스트가 22개였다.
「막혔던 지점」과 Learnings 가 나오는 재료가 바로 이것이다.

단, **시그니처는 좁게** 유지한다. `stderr` 가 비어있지 않은 673건은 **전부**
harness 의 `Shell cwd was reset` 노이즈였고, 넓은 `/error:/` 는 `ls` 탐색과 grep
출력을 긁어온다. 추론으로 잡은 것은 `inferred` 플래그로 표시해 렌더링에서 구분하고,
개수도 `max_errors` 로 따로 제한한다 — 로그 한 번에 섹션이 잠기면 안 된다.

### 16. 명령 allowlist 는 도메인을 모른다
빌드 도구에서 멈춘 allowlist 가 bash 8706건 중 6027건을 버렸다(ssh 1242, python 491,
curl 86). 명령 0줄인 다이제스트가 133개 중 29개였고, 그중 하나는 1468개를 돌린
세션이었다. **플릿 작업의 본체는 ssh 다.** ops 동사를 기본 목록에 넣고, 그래도 모를
도구(daml/canton/sui/vast/orca…)는 config 의 `extra_commands` 정규식으로 연다.
잘못된 정규식은 캡처를 죽이지 않고 기본 목록으로 degrade 한다.

### 17. 합성된 다이제스트는 안 건드리되, 세션 후반을 버리지도 않는다
PreCompact 로 다이제스트가 생기고 → 사용자가 `/dev-journal` 을 돌리고 → 세션이
계속되면, SessionEnd 캡처가 "이미 합성됨"에 막혀 **세션 후반부가 통째로 사라졌다.**
이제 더 풍부한 경우에 한해 `--part2` 파일로 옆에 쓴다. 규칙:
- 합성된 파일은 절대 덮지 않는다(원래 계약 유지).
- part 파일은 transcript **전체**를 다시 읽으므로 기존 노트와 겹친다 — 상단 경고와
  `part: N` frontmatter 로 스킬에 알린다. 스킬은 새 세션 노트를 만들지 않고 기존
  노트를 확장한다.
- part 파일은 `twins()` 대상이 아니다. 쌍둥이 청소가 합성된 원본을 지우면 안 된다.

### 18. 마스킹 키워드는 **더 긴 단어의 접두사면** 무시한다
`auth` 가 `authorized`/`authentication`, `secret` 이 `secretary`, `token` 이
`tokenizer` 에 걸려 실제 내용을 지웠다(볼트에 `com.digitalasset.auth.TokenService:
«redacted»` 가 남아있다). 키워드 뒤에 소문자가 이어지면 다른 단어로 보고 건너뛴다 —
단 복수형 `s` 와 `authorization` 은 예외다. 그리고 플래그 정규식의 `-` 앞에는 단어
경계를 요구한다: 없으면 `ssh-keygen` 의 `-keygen` 이 플래그로 잡혀 **다음 인자**가
지워진다.

카멜케이스(`TokenService`)는 일부러 남겨뒀다. 이걸 막으려면 `{"secretValue": …}` 도
같이 놓치게 되는데, **마스킹 실패가 오탐보다 나쁘다.**

### 19. 테스트는 실제 볼트에 닿을 수 없어야 한다
`run_capture()` 를 `home=` 없이 부른 케이스가 실제 홈 → 실제 config → 실제 볼트에
`session_id: "garbage"` 다이제스트를 썼고, 그게 노트로까지 합성됐다. `HOME` 만으로는
부족하다 — `OBSIDIAN_VAULT` 와 `OBSIDIAN_DEV_JOURNAL_CONFIG` 가 그걸 덮는다.
`sandbox_env()` 가 셋 다 처리하고, `test_no_real_vault_writes()` 가 그 계약을 지킨다.

### 20. `_raw` 는 큐지 보관소가 아니다
합성이 끝난 다이제스트 133개(846KB)가 볼트 안에 영구히 쌓여 Obsidian 검색과 그래프를
오염시켰다. `prune` 은 **`processed` 만** 대상으로 하고(`held`/`unprocessed` 는 절대),
기본 동작은 삭제가 아니라 **볼트 밖 archive 로 이동**이다 — 검색에서 사라지는 게 목적이고
되돌릴 수 있어야 한다. 나이는 캡처일이 아니라 `processed:` 날짜로 센다.

### 21. `host:` 는 캡처 시점에만 알 수 있다
여러 볼트를 나중에 하나로 합칠 때(머신별/회사·개인별) 다이제스트의 출처를 알려주는
유일한 필드다. `session_id` 는 UUID고 `cwd` 는 머신을 말해주지 않는다. **소급이
불가능하므로** 나중에 "필요해지면 넣자"가 성립하지 않는다 — 그래서 쓸 일이 없어
보이는 지금 넣어뒀다.

두 가지 함정: `socket.gethostname()` 은 네트워크에 따라 `mbp.local`/`mbp.lan` 을
오가므로 첫 라벨만 쓴다. 그리고 값이 비었을 때 `slugify()` 를 그냥 통과시키면
fallback 인 `"session"` 이 **모든 다이제스트의 호스트가 되어버린다** — 없는 정보를
지어내는 쪽이 빈 값보다 나쁘다. `host_name()` 이 빈 문자열을 명시적으로 돌려준다.

합성 노트로도 넘겨야 한다. `_raw` 는 `prune` 으로 볼트를 떠나므로(20번), 세션 노트가
남는 유일한 기록이다.

## 파일 지도

| 파일 | 역할 |
| --- | --- |
| `scripts/on_session_end.sh` | 훅 진입점. 즉시 반환 + 로그 회전 + 고아 페이로드 청소 |
| `scripts/capture_session.py` | transcript → 다이제스트. LLM 미사용. 성능 민감 |
| `scripts/journal_util.py` | CLI: init/doctor/pending/cat/done/hold/unhold/index/prune/backfill/stats |
| `skills/dev-journal/` | 합성 스킬 + `references/note-templates.md` |
| `skills/dev-journal-setup/` | 볼트 연결, 진단, backfill 안내 |
| `skills/career-review/` | 이력서 불릿·성과 리뷰·성장 회고 |
| `hooks/hooks.json` | SessionEnd + PreCompact. `${CLAUDE_PLUGIN_ROOT}` 사용 |
| `tests/test_capture.py` | 회귀 스위트 184건. 의존성 없음 |

상태는 전부 홈 디렉터리에 있다: `~/.claude/obsidian-dev-journal/`(config.json,
logs/, pending/, staged/). 리포에는 코드만 들어간다.

## 작업 규칙

- **수정 후 반드시** `python3 tests/test_capture.py` — 184건 전부 통과해야 한다.
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
