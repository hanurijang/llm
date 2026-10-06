# 세션 규칙

이 폴더의 `llm` 세션에 적용되는 프로젝트 규칙입니다. 대화 기록과 별개로
저장소에 남으며, 매 요청의 시스템 프롬프트에 포함됩니다.

## 프로젝트

- **llm**: DeepSeek CLI 코딩 에이전트.
- 구성: `llm`(bash 진입점) + `agent.py`(CLI·에이전트 루프·UI) + `llmlib/`(분리 모듈:
  `shell_policy` 셸 안전 분류, `guard` 비밀 보호, `netretry` API 재시도,
  `filestate` 편집 추적·undo, `conversation` 중단·결과 정리, `context` @멘션·
  CLAUDE.md, `web` web_fetch, `bgshell` 백그라운드 셸,
  `mdrender` 터미널용 Markdown 렌더러, `shelllog` 셸 허용/실행 로그,
  `cliargs` 서브커맨드→옵션 펼치기·기본 실행 형태). 새 기능은
  가능하면 `llmlib/`에 순수 함수로 두고 `agent.py`는 연결만 한다.
- **표준 라이브러리만 사용**. 외부 의존성 추가 금지.
- Python 3.10+ 문법 사용.

## CLI 표면 (필수)

- **인자 없는 `llm`(TTY)과 `llm -i`·`llm repl`·`llm agent`는 모두 대화형(REPL)** 이다
  (계산은 `wants_repl(once, stdin_tty, stdout_tty)`; 파이프·리다이렉션은 1턴).
  `llm "질문"`은 대화형 **첫 턴**, 1턴 실행은 `llm ask "질문"`(= `--once`).
  `cmd_agent`는 에이전트 루프 하나를 돌리고 `cmd_repl`이 그걸 반복한다 — 두 이름을
  마로게 두지 않는다.
- **주제별 서브커맨드**는 **첫 번째 인자**일 때만 펴진다: `ask|once`·`chat`·
  `plan`·`repl|agent|i`·`sync`·`undo`·`balance`·`rules show|init`·
  `shell log [N|clear]`·`shell allow [clear]`·
  `session ls|adopt|view|clear|delete|compact|run|ref [이름]`.
  변환은 순수 함수 `llmlib/cliargs.expand(argv)→(argv, err)` 하나만 쓰고
  (`agent.py`의 `expand_cli_argv`가 얇은 중계), `agent.py`의 `main`/`argparse`는
  펴진 플래그만 본다. 잘못된 사용법은 `err`(사용법 한 줄)을 돌려 `--help`로 안내한다.
- **옛 플래그는 별짐으로 유지하되 `argparse.SUPPRESS`로 `--help`에서 숨긴다**
  (`--allow-all`, `--auto-workspace`, `--allow-list`/`--allow-clear`,
  `--shell-log`/`--shell-log-clear`, `--init-rules`/`--rules`/`--summarize`,
  `--sync-history`, `--view`/`--run`/`--clear`/`--delete`/`--session-adopt`).
  제거는 별도 단계(2단계)에서 한다.
- 이 표면(서브커맨드 목록·기본 실행 형태·숨긴 별짐)을 바꿀 때는
  `llmlib/cliargs.py`, `agent.py`(`expand_cli_argv`·`main`·`parser` epilog),
  `tests/test_cliargs.py`, `tests/test_phase1.py`(`test_expand_cli_argv`·
  `test_help_has_subcommands`), README 「사용법 (CLI 옵션)」·「최소 사용법」을 함께 고친다.

## 코딩 규칙

- 변경은 **최소**로, 기존 구조와 스타일을 유지한다.
- 기존 파일 편집은 **먼저 `read`** 한 뒤 `edit`(action=replace, 여러 곳은 `edits`),
  새 파일은 `edit`(action=write).
- 탐색은 `find`(mode=list|glob|grep), 읽기는 `read`.
- 사용자 대면 메시지는 공통 UI 헬퍼(`print_title`, `print_info`, `print_ok`,
  `print_warn`, `print_error`, `print_phase`, `rule`)를 사용해 일관성을 유지한다.
- 최종 답변·규칙 문서처럼 Markdown 본문을 보여줄 때는 `render_markdown`
  (=`llmlib/mdrender.py`)을 쓴다. 외부 도구(glow 등)에 기대지 말고, 파이프·
  `LLM_RAW`·비TTY에서는 원문을 그대로 출력하는 현재 동작을 유지한다.
- 터미널 정렬이 필요한 곳은 한글 등 wide 문자를 고려해 `display_width`/`pad`를
  사용한다.
- 편집 후 `python3 -m py_compile agent.py llmlib/*.py`와
  `python3 -m pytest -q tests`로 검증한다.

## 작업 계획(todo) 워크플로 (필수)

- **작업을 시작하면 `todo` 도구를 먼저 호출**해 `goal`(이번 작업의 목표 또는
  결정한 조치를 한두 문장으로 요약)과 전체 계획(짧은 항목 목록)을 세운다.
  터미널에는 **목표 줄(`🎯 …`)이 목록 상단**에, 그 아래에 `📋 작업 계획` 목록이
  표시된다.
- **하나의 job으로 끝까지 이어간다.** 같은 계획을 유지한 채 항목의
  `status`(pending/in_progress/done)만 갱신하며 진행한다. 작업이 길어져도 중간에
  계획을 새로 만들지 않는다.
- **대화 요약(압축)을 넘어서도 이어진다.** 요약 시 진행 중인 계획이 요약문에
  함께 실리므로(`todo_plan_text`/`todo_stamp`), 요약 이후·다음 실행에서도 요약에
  남은 계획을 그대로 이어간다(`restore_todo`). 요약 뒤에 todo를 새로 시작하지 않는다.
- **끝난 계획은 되살리지 않는다.** 모든 항목이 완료(done)된 계획은 복원·이어가기
  대상이 아니며(`todo_active`), 저장할 때마다 요약 헤드의 스냅샷을 최신 계획으로
  갱신하고 계획이 끝났으면 계획 블록·스냅샷을 헤드에서 걷어낸다
  (`refresh_todo_snapshot`). 옛 목표(`🎯 …`)가 새 작업의 목표처럼 자동 설정되는
  것을 막기 위함이다.
- **상한 초과도 계속 하나의 job으로 처리한다.** 항목이 `TODO_MAX_ITEMS`(50)개를
  넘으면 화면에는 **완료 항목부터 접어** 보여주고(`… 완료 N개 접음 (전체 M개 항목,
  계속 이어감)`), 진행률·에이전트가 보는 목록은 전체 개수를 유지한다. 접혔다고
  작업을 쪼개거나 새 계획으로 넘어가지 않는다.
- 이 동작을 바꿀 때는 `agent.py`의 `TODO_*` 상수·`render_todo`/`tool_todo`/
  `todo_active`/`todo_plan_text`/`todo_stamp`/`restore_todo`/
  `refresh_todo_snapshot`와 README 「작업 계획(todo) 표시」
  섹션을 함께 갱신한다.

## 대화 기록 위치와 동기화 (필수)

- 대화 기록은 **작업 폴더 안 `<작업폴더>/.llm/history_<세션>.json`**(기본)에
  저장한다. `LLM_HISTORY=home`이면 예전처럼 `~/.local/share/llm/`에 저장한다
  (이 폴백은 유지한다).
- **기기 로컬 정보(`bindings.json`, `shell_allow_*.json`,
  `shell_log_*.jsonl`, `rates.json`, `autopull.json`)는 항상 홈**에 둔다.
  대화 기록 위치 변경과 무관하다.
- 홈에만 있던 기록은 그 폴더에서 처음 실행할 때 `.llm/`으로 **한 번** 옮기고,
  양쪽에 있으면 더 긴 쪽을 채택하며 홈 원본은 `*.migrated`로 남긴다.
- **대화 기록 자동 커밋·push는 기본 꺼짐.** `LLM_AUTO_SYNC=1`일 때만 실행
  종료 시 `auto_sync_history`가 기록·요약만 커밋 → **ff-only pull** → `push`.
  평소에는 `llm sync`(=`--sync-history`)로 직접 올린다. rebase 하지 않는다.
- **다른 PC에서 이어갈 때는 첫 질의 전에 원격 최신 기록을 먼저 받아온다**
  (`maybe_auto_pull` → `history_receive_remote`: fetch 후 ff-only만).
  시작 헤더의 **이어가기** 행으로 로컬 턴·미커밋·원격 ahead/behind를 보여 준다.
  실패·갈라짐·dirty 면 경고만 하고 **로컬 기록으로 계속**.
  끄는 방법은 `LLM_AUTO_PULL=0` 또는 `--no-pull`.
- clone 직후 `.llm/history_*`가 하나이거나 폴더명과 같으면 **세션 자동 바인딩**.
  여러 개면 `llm session adopt`.
- 대화형: `llm`(인자 없음) / `llm -i` / `llm repl` / `llm agent` (같은 프로세스 다턴, 종료 시 sync 1회).
- `.llm/`이 `.gitignore`에 있으면 에러로 안내하고 중단한다.
- `LLM_HISTORY=home`으로 되돌리면 홈의 `*.migrated` 기록을 다시 꺼내 온다.
- 이 동작을 바꿀 때는 `agent.py`의 `HISTORY_*`/`REPO_HISTORY_DIR_NAME`/
  `history_scope_dir`/`set_history_base`/`find_history_file`/
  `migrate_legacy_session`/`cmd_sync_history`/`auto_sync_enabled`/
  `auto_sync_history`/`AUTO_SYNC_ENV`/`auto_pull_enabled`/`auto_pull_interval`/
  `maybe_auto_pull`/`AUTO_PULL_*`와 README 「대화 기록 동기화」·
  「파일/데이터 위치」·「설정과 환경변수」 표를 함께 갱신한다.

## 셸 확인 기본값과 로그 (필수)

- **대화형(`llm`/`llm -i`/`llm repl`/`llm agent`)의 셸 확인 기본은 `readonly`(읽기 전용 자동)** 이다
  (`REPL_DEFAULT_ASK_MODE`, `repl_ask_mode`). 읽기 전용 명령은 조용히 실행하고
  그 밖은 확인한다. 1턴 실행(`llm ask`)은 기존대로 `safe`(작업 폴더 안 자동)이며,
  `--safe`/`--trust`/`--yes`/`--confirm-all`이나 `LLM_*` 환경변수를 주면 그쪽을
  우선한다(repl 기본을 덮지 않는다).
- **권한 상승(`sudo`/`su`/`doas`/`pkexec`) 조각은 모드·허용 목록·`a`/`all`·
  `--trust`와 무관하게 매번 확인**한다(`ask_shell_privilege`, `[y/N]`). 프롬프트에
  목적(`privilege_purpose`)·실제 실행 명령(`privilege_target`)을 보여 주고,
  거부하면 실행하지 않는다.
- repl 시작 시 **설정 상태를 시작 헤더의 행으로** 표시한다(확인 모드 · 완화 옵션 ·
  로그 파일 경로/기록 여부 · 조회법). 헤더는 `print_banner`(제목 줄 + `header_line`
  행: 세션·폴더·셸·설정) + `print_continue_status`(이어가기 행) + `cmd_repl` 의
  `완화`/`로그`/`도움말` 행으로 구성하고, 라벨 폭은 `HEADER_LABEL_WIDTH` 로
  맞춘다(`display_width`/`pad` 사용). 행 구성·문구를 바꾸면 README「시작 헤더」
  예시도 함께 고친다.
- 확인 프롬프트에서 허용해 **묻지 않고 실행된 셸 명령은 세션별 로그에 남긴다**
  (`shelllog.ShellLog` → `~/.local/share/llm/shell_log_<세션>.jsonl`, 종류
  `auto/a/all/y/n`, 최근 500건). 로그 쓰기 실패는 셸 실행을 막지 않는다.
- 조회: `llm shell log [N]`(`--shell-log`), REPL `/log [N]`;
  비우기: `llm shell log clear`(`--shell-log-clear`)/`/log clear`.
  `LLM_SHELL_LOG=0`이면 새로 남기지 않는다.
- 이 동작을 바꿀 때는 `agent.py`의 `shell_log_file`/`shell_log_enabled`/
  `shell_log`/`cmd_shell_log`/`REPL_DEFAULT_ASK_MODE`/`repl_ask_mode`/
  `ask_shell_privilege`, `llmlib/shell_policy.py`(권한 상승 판별·목적 요약),
  `llmlib/shelllog.py`와 README 「셸 실행과 안전장치」(「REPL 기본 확인 모드와
  셸 로그」)·「설정과 환경변수」·「파일/데이터 위치」를 함께 갱신한다.

## README 갱신 (필수)

- **`agent.py`/`llm`의 기능, CLI 옵션, 기본값, 동작, 파일/데이터 위치가
  바뀌면 `README.md`를 즉시(같은 작업에서) 업데이트한다.**
- 새 CLI 옵션 추가 → README의 "사용법 (CLI 옵션)" 표에 추가.
- 기본값/상수 변경(모델, 최대 단계, 제한 바이트, 히스토리 경로 등) → 관련
  표/설명 수정.
- 도구 추가/제거 또는 인자 변경 → "에이전트 도구" 표 수정.
- 안전장치(차단 패턴, 비밀 파일 목록, 확인 절차) 변경 → "셸 실행과
  안전장치" 수정.
- 환경변수 추가/변경 → "설정과 환경변수" 표 수정.
- README 변경이 누락됐다고 판단되면 먼저 README를 갱신한 뒤 다른 작업을
  마무리한다.

## 버전 관리 (필수)

- **사용자에게 보이는 기능·기본값·CLI가 바뀌면 `agent.py`의 `VERSION`을 함께
  올린다.** 같은 작업 커밋에 포함시키고, 버전만 올리는 별도 커밋으로 미루지
  않는다.
  - 새 기능·옵션·모듈·기본값 변경 → 마이너(`0.8.0`).
  - 버그 수정·문구/문서만 변경 → 패치(`0.8.1`).
- 버전을 올릴 때 함께 갱신할 것:
  - `agent.py`의 `VERSION`(시작 배너·머리표시).
  - README 머리의 `**버전:** v…` 줄과 `## v<마이너> 변경 사항` 표(직전 버전 →
    현재 버전 비교 행 추가. 옛 표는 그대로 두고 새 절을 위에 붙인다).
  - README 안의 버전 예시(`llm` 실행 예·배너 출력)와 `tests/test_phase1.py`의
    `test_version`/`test_version_matches_readme`.
- 태그는 만들지 않고 `git log`의 커밋 제목으로 이력을 남긴다.

## 체크포인트와 롤백 (필수)

- **코드 수정을 시작하기 전에 원격 최신 상태를 먼저 받는다**:
  `git pull --rebase --autostash`. (코드 브랜치 한정. 대화 기록 동기화
  `llm sync`는 위 규칙대로 ff-only 이며 rebase 하지 않는다 — 둘은 별개다.)
  실패(충돌·인증 등)하면 수정을 시작하지 말고 원인을 사용자에게 알린다.
- 이어서 현재 상태를 커밋해 안전 지점(checkpoint)을 만든다. 이미 커밋된
  상태면 생략 가능. 예:
  `git add -A && git commit -m "checkpoint: <작업 요약>"`.
- 수정이 끝나면 **반드시 `python3 -m py_compile agent.py llmlib/*.py`와
  `python3 -m pytest -q tests`로 검증**한다.
- **검증을 통과하면 한 줄 요약으로 커밋하고 `git push`까지 수행한다**(자동).
  push가 실패하면 `--force` 등으로 우회하지 말고 원인을 알린다.
- **검증에 실패하거나 수정이 중단/실패하면 즉시 되돌리고 push하지 않는다**:
  `git restore agent.py`(또는 작업한 파일).
- 되돌린 뒤에는 사용자에게 **어떤 파일을 왜 되돌렸는지** 알린다.
- `agent.py`는 편집 대상이자 실행 대상이므로, 문법 오류 상태로 남기지 않는다.
- 대화 기록 자동 커밋·push는 **기본 꺼짐**(`LLM_AUTO_SYNC=1`일 때만 종료 시 실행).
  꺼져 있으면 대화 기록은 로컬에만 있으므로 필요할 때 `llm sync`로 올린다.
- 커밋 메시지는 무엇을 바꿨는지 한 줄로 요약한다.

## 답변 마무리 — 동기화 상태 표시 (필수)

- **답변(최종 응답)의 마지막 줄에 git 동기화 상태를 한 줄로 보여준다.** 답변을
  쓰기 직전의 실제 상태를 확인해서 적는다: `git status --short`,
  `git log --oneline origin/main..HEAD`(또는 `git rev-list --count`),
  push한 커밋의 변경 파일(`git show --stat --oneline <해시>`).
- 표시 내용: **push 여부**와 **push한 커밋(짧은 해시 + 제목)**, 그 커밋이 바꾼
  **코드/파일 목록**(대화 기록 JSON만 바뀐 경우와 코드가 바뀐 경우를 구분),
  그리고 대화 기록 동기화 상태(자동 동기화가 끝나면 올라가고
  `LLM_AUTO_SYNC=0`이면 아직 로컬이라는 사실까지). 예:
  - `🔁 동기화: 코드 push 완료 · aeff65e "fix(todo): …" · agent.py 85±, README.md 11±, .llm/rules.md 8± · 대화 기록은 로컬(llm sync 로 올림)`
  - `🔁 동기화: 규칙 push 완료 · d721695 "rules: …" · .llm/rules.md +15 · 대화 기록은 LLM_AUTO_SYNC=1 이면 종료 시 자동 push`
  - `🔁 동기화: 변경 없음(원격과 동일 · 코드 변경 없음, 대화 기록은 로컬)`
  - `🔁 동기화: push 실패(<원인>) · 로컬 커밋만 있음(<해시> "…")`
- 파일 목록은 **코드·문서·규칙 파일을 먼저** 적고, 대화 기록/요약(`.llm/`)은
  "대화 기록 …" 항목으로 묶어 표현한다(길어지면 대표 파일만 적어도 된다).
- push를 못 했거나 실패했으면 **숨기지 않고 이유와 함께** 적는다(우회 push 금지).
- 이 규칙은 `agent.py`가 아니라 **답변 형식**에 관한 것이므로, 바꿀 때는
  README 「응답 표시와 사고 과정」의 답변 골격 설명도 함께 갱신한다.

## 셀프테스트 잔여 파일 (삭제 상시 허용)

- `llm` 자체를 시험하면서 만든 세션 흔적은 **확인 없이 삭제해도 된다**
  (사용자 상시 허용). 대상은 `~/.local/share/llm/` 안에서 세션 이름이
  `*selftest*`, `ztest*`, `zshort*`, `zautotest*`, `zptyselftest*` 패턴인 것:
  - `history_<세션>.json`, `shell_allow_<세션>.json`, `shell_log_<세션>.jsonl`
  - `summaries/<세션>/`
  - `bindings.json`의 해당 폴더 ↔ 세션 매핑(폴더가 사라진 항목 포함)
- 단, **현재 실행 중인 세션**(`current_session` 값)의 파일은 아직 지우지 않는다
  (다음 저장 때 다시 생성됨). 세션이 끝난 뒤 정리한다.
- 기본 저장 위치가 `.llm/`이므로 **시험용 작업 폴더 안의
  `.llm/history_<세션>.json`·`.llm/summaries/<세션>/`도 같은 패턴이면 대상**이다
  (`/tmp/...` 하네스 폴더는 통째로 지우면 된다).
- 삭제 전 대상 목록을 한 번 보여주고, 삭제 후 무엇을 지웠는지 요약해 보고한다.
- 허용 범위는 위 self-test 패턴뿐이다. 그 밖의 `~/.local/share/llm/` 파일
  (실사용 세션 기록·요약)은 이 규칙으로 지우지 않는다.

## 하지 말 것

- 표준 라이브러리 외 의존성 추가.
- `.env`, API 키, 자격 증명 파일 내용 출력.
- 워크스페이스 밖 경로 접근/수정. (단, 위 "셀프테스트 잔여 파일" 범위는 허용)
- README를 갱신하지 않고 기능만 변경.
