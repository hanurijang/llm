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

## CLI 표면·대화 UI (필수)

- 외부 주요 명령: `llm`(TTY REPL), `llm "질문"`(TTY 포함 1회 실행), `llm bal`(잔액), `llm help`(사용법), `llm session`(대화형 세션 관리 메뉴). 질문 인자가 없는 비대화형 실행은 CLI 도움말을 표시한다.
- 대화형 내부 명령: `/agent`, `/chat`, `/plan`(읽기 전용), `/undo`, `/bal`, `/prompt`, `/help`, `/exit`. 모드 전환은 현재 대화 기록을 유지한다.
- `/prompt`는 사용자 로컬 프롬프트를 관리한다. System=`~/.local/share/llm/system.md`, Project=`<작업폴더>/.llm/rules.md`, Session=`<기록폴더>/prompts/<세션>.md`. 기본 안전 지침은 사용자 프롬프트로 제거하지 않는다.
- `llm session`은 로컬 세션 목록·조회·연결·초기화·삭제를 메뉴로 제공한다. 폴더별 세션 소유권을 유지하며 파괴적 변경에는 확인을 받는다.
- 기존 외부 명령·플래그는 호환을 위해 일부 남길 수 있으나 기본 도움말에는 나타내지 않는다. 더 이상 대화 기록을 Git에 동기화하지 않으며 `llm sync`는 거부한다.
- 시스템은 무거운 외부 UI 의존성을 추가하지 않고 ANSI·readline·내장 Markdown 렌더러를 유지한다. 비대화형 환경에서는 인터랙티브 메뉴를 열지 않는다.
- CLI·프롬프트 구조를 변경할 때 `agent.py`, `llmlib/cliargs.py`, 관련 테스트, README를 함께 갱신한다.

## 진입 래퍼(`llm`) 이식성 (필수)

- **`llm`(bash 래퍼)은 `readlink -f` 에 의존하지 않는다.** macOS/BSD 의 readlink 에는
  `-f` 가 없어서 그것에 기대면 `~/bin/llm` 심볼릭 링크가 깨진다(`python3: can't open
  file '~/bin/agent.py'`). `BASH_SOURCE` + `readlink` 루프로 링크를 직접 따라간다
  (상대 링크·다단계 링크 지원, 최대 40 hop).
- **실행할 Python 은 `LLM_PYTHON`(기본 `python3`)으로 바꾼다.** 못 찾으면 실행기를
  알려 주는 안내 후 exit 127, 래퍼와 `agent.py` 가 같은 폴더에 없으면 안내 후 exit 1.
- 이 동작을 바꿀 때는 `llm`(래퍼), `tests/test_launcher.py`, README 「설치 · 실행 파일
  배치」·「다른 PC에서 시작하기(macOS·다른 환경에서 쓸 때)」·「설정과 환경변수」
  (`LLM_PYTHON`)·「준비물 체크」를 함께 갱신한다.

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

## 대화 기록 위치와 보안 (필수)

- **대화 내용과 세션 요약은 로컬 PC 전용**이다. `llm/.env`의 `LLM_HISTORY_DIR`에 저장 경로를 설정한다. 빈 값이면 기본 위치 `<작업폴더>/.llm/history_<세션>.json` 및 `.llm/summaries/`를 사용한다. `LLM_HISTORY_DIR`이 비어 있고 `LLM_HISTORY=home`이면 예전처럼 `~/.local/share/llm/`에 저장한다.
- `LLM_HISTORY_DIR`은 절대경로를 권장하며, 상대경로는 현재 작업 폴더 기준이다. 작업 폴더별 세션 이름을 구분하며 공유 로컬 경로에서 다른 프로젝트의 세션을 잘못 자동 연결하지 않는다. 저장 경로는 Git 저장소 바깥으로 지정한다.
- `.llm/rules.md`만 프로젝트 정책으로 Git에서 관리한다. 대화 기록과 요약은 `.gitignore`로 제외하며 **커밋하거나 GitHub로 Push하지 않는다**.
- `llm sync`(`--sync-history`)는 오류를 안내하고 종료한다. `LLM_AUTO_SYNC=1` / `LLM_AUTO_PULL=1`을 지정해도 대화 기록의 자동 Push·Pull은 수행하지 않는다.
- 세션 목록·조회·요약·참조·삭제·복원은 모두 이 PC의 로컬 파일과 바인딩만 사용한다. 다른 PC에서 이어가기, 원격 기록 수신, Git 기반 세션 동기화는 제공하지 않는다.
- 기존 로컬 `.llm/` 기록은 그대로 사용한다. 기존 PC에서 이미 추적된 세션 파일이 있는 경우 `git pull` 전에 별도 로컬 폴더로 복사하여 보존한다.
- 개발 코드와 검토된 문서만 GitHub에 Push한다. 개인 대화, 인증정보, 세션 요약, 실행 로그는 로컬에서만 보관한다.
- 로컬 기록은 필요한 경우 Git과 무관하게 다른 디스크에 백업한다. 기록 위치·동기화 정책 변경 시 README와 관련 테스트를 함께 갱신한다.

## REPL 맥락·API 비용·셸 승인 (필수)

- REPL 기본 셸 정책은 `safe`(`outside`)이며 작업 폴더의 안전한 명령은 자동 실행한다. 관리자 권한 상승, 설치, 위험하거나 영향 범위가 큰 명령은 사용자에게 확인한다. `--trust`, `--yes` 등 기존 고급 옵션은 유지하되 기본 사용법에서 강조하지 않는다.
- `sudo`/`su`/`doas`/`pkexec` 같은 권한 상승은 `--yes`를 명시하지 않는 한 매번 개별 확인하며 자동 허용 목록이나 전체 허용으로 건너뛰지 않는다. 비밀값 노출 가능성이 있는 명령은 별도 보호한다.
- 권한 허용·거부 기록은 PC의 `~/.local/share/llm/shell_log_<세션>.jsonl`에만 남긴다. 반복 확인을 피하기 위해 기존 세션별 허용 목록을 유지한다.
- 맥락 관리에서는 `clear_old_tool_results`로 오래된 도구 결과를 먼저 줄인 뒤 `maybe_summarize`를 호출한다. 최근 메시지·도구 호출/결과 결합 상태를 보존하고, 요약 직후 불필요한 재요약을 금지한다. 극단적인 토큰 초과는 예외로 하되 실제 줄일 맥락이 충분해야 한다.
- 요약은 DeepSeek API 비용을 발생시키므로 요청마다 반복하지 않는다. 요약·원본 스냅샷은 로컬 저장만 허용하고, 요약에 따른 비용 절감 수치는 실제 토큰 사용량으로 확인한다.
- 관련 코드 변경 시 `agent.py`, `llmlib/conversation.py`, 관련 테스트 및 README를 함께 점검한다.

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

## 답변 마무리 — 소스코드 동기화 상태 표시 (필수)

- 최종 응답의 마지막 줄에는 실제 코드 변경의 git commit/push 여부를 정확히 적는다.
- **대화 기록은 로컬 전용이며 push하지 않는다.** 대화 기록의 원격 동기화를 권하거나 수행하지 않는다.
- push 실패나 미완료는 숨기지 않고 이유를 명시한다.

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
