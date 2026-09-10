# llm — DeepSeek CLI 에이전트

터미널에서 DeepSeek API를 사용해 파일 읽기/편집, 검색, 셸 실행을 수행하는
경량 코딩 에이전트입니다. Python 표준 라이브러리만으로 동작하며, 별도의
패키지 설치가 필요 없습니다.

- **에이전트 모드**: 도구(파일/검색/셸)를 스스로 호출하며 작업을 완수
- **채팅 모드**: 도구 없이 순수 대화
- **세션**: 폴더별로 대화 기록을 자동 연결·보존
- **세션 규칙**: 프로젝트별 지침을 `.llm/rules.md`에 남겨 매 요청에 반영
- **진행 표시 위주 출력**: 단계 번호(예: `── 3/40단계 ──`)와 도구 호출만 보여주고, 사고 과정 원문은 기본으로 감춥니다(`--show-thinking`으로 확인)
- **읽기 전용 셸 자동 실행**: `ls`, `cat`, `git status` 등 안전한 명령은 확인 없이 실행

---

## 목차

1. [요구 사항](#요구-사항)
2. [설치](#설치)
3. [빠른 시작](#빠른-시작)
4. [사용법 (CLI 옵션)](#사용법-cli-옵션)
5. [세션과 폴더 매핑](#세션과-폴더-매핑)
6. [대화 요약과 백업](#대화-요약과-백업)
7. [응답 표시와 사고 과정](#응답-표시와-사고-과정)
8. [세션 규칙](#세션-규칙)
9. [에이전트 도구](#에이전트-도구)
10. [셸 실행과 안전장치](#셸-실행과-안전장치)
11. [설정과 환경변수](#설정과-환경변수)
12. [파일/데이터 위치](#파일데이터-위치)
13. [아키텍처](#아키텍처)
14. [문제 해결](#문제-해결)
15. [개발 규칙](#개발-규칙)
16. [라이선스](#라이선스)

---

## 요구 사항

| 항목 | 설명 |
|------|------|
| Python | 3.10 이상 (`str \| None` 등 최신 타입 문법 사용) |
| 셸 | bash (셸 도구는 `/bin/bash`로 실행) |
| API 키 | `DEEPSEEK_API_KEY` 필요 |
| glow(선택) | 설치되어 있으면 최종 답변을 Markdown으로 렌더링. 없으면 원문 출력 |

의존 패키지는 없습니다. `urllib`, `json`, `subprocess` 등 표준 라이브러리만
사용합니다.

---

## 설치

### 1. API 키 설정

`agent.py`는 시작 시 `/etc/env/.env`를 자동으로 읽습니다. 아래 형식으로
키를 저장하세요.

```dotenv
DEEPSEEK_API_KEY=sk-...
```

`/etc/env/.env`가 없으면 일반 환경변수로 지정해도 됩니다.

```bash
export DEEPSEEK_API_KEY=sk-...
```

### 2. 실행 파일 배치

`llm`(bash 래퍼)과 `agent.py`가 같은 폴더에 있어야 합니다.
`llm` 래퍼는 자기 위치를 기준으로 `agent.py`를 찾습니다.

```bash
# 저장소 위치
/home/<user>/codespace/userscript/llm/
├── llm          # 실행 진입점 (bash)
└── agent.py     # 실제 로직 (python)
```

### 3. 전역 명령으로 등록 (권장)

`~/bin`은 사용자 전역 명령을 두는 관례적인 경로입니다. 여기에 심볼릭 링크를
걸고 PATH에 등록하면 어디서든 `llm`으로 실행할 수 있습니다.

```bash
mkdir -p ~/bin
ln -sf /home/<user>/codespace/userscript/llm/llm ~/bin/llm
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

동작 확인:

```bash
which llm          # → /home/<user>/bin/llm
readlink ~/bin/llm # → /home/<user>/codespace/userscript/llm/llm
llm                # 인자 없이 실행하면 상태/세션 목록 표시
```

> `~/bin`이 PATH에 없으면 새 셸에서 `llm`을 찾지 못합니다. 위 `export PATH`
> 한 줄이 영구 등록의 핵심입니다.

---

## 빠른 시작

```bash
# 이 폴더에서 에이전트에게 질문 (도구 자동 사용)
llm "이 프로젝트 구조를 요약해줘"

# 파일 내용을 읽고 고치도록 요청
llm "agent.py의 확인 프롬프트 문구를 더 친절하게 다듬어줘"

# 도구 없이 순수 채팅
llm --chat "파이썬에서 리스트와 튜플의 차이는?"

# 셸 명령 확인 생략 (자동화/신뢰 환경)
llm --yes "테스트 돌리고 실패하면 원인 요약해줘"

# 작업 폴더 안에서 끝나는 명령은 확인 없이 실행 (폴더 밖/위험 명령만 확인)
llm --auto-workspace "빌드 정리하고 린트·포맷까지 돌려줘"

# 사고 과정 없이 빠르게 답만 받기
llm --no-thinking --chat "리스트와 튜플 차이는?"

# 모델의 사고 과정 원문까지 보고 싶을 때 (기본은 단계/도구 진행만 표시)
llm --show-thinking "이 코드의 버그 원인을 추적해줘"

# 읽기 전용 명령도 매번 확인 (자동 실행 비활성)
llm --confirm-all "이 폴더 상태를 점검해줘"

# 이 세션에서 자동 허용한 셸 명령 목록 보기 / 비우기
llm --allow-list
llm --allow-clear
```

세션은 실행한 폴더에 자동으로 연결됩니다. 같은 폴더에서 다시 `llm`을
호출하면 이전 대화 맥락이 이어집니다.

---

## 사용법 (CLI 옵션)

### 기본 실행

| 명령 | 설명 |
|------|------|
| `llm "질문"` | 현재 폴더 세션으로 에이전트 실행 |
| `llm --chat "질문"` | 도구 없이 순수 채팅 |
| `llm` | 상태/세션 목록/사용법 표시 |

### 옵션

| 옵션 | 설명 |
|------|------|
| `-s, --session <이름>` | 이 폴더를 지정한 세션에 매핑 |
| `--chat` | 도구 없이 순수 채팅 모드 |
| `-y, --yes` | 셸 명령 실행 확인 생략 |
| `--auto-workspace` | 작업 폴더 안에서 끝나는 명령은 확인 없이 실행 (`LLM_AUTO_WORKSPACE=1`이면 기본 활성) |
| `--root <경로>` | 작업 디렉터리 지정 (기본: 현재 폴더) |
| `--model <이름>` | 모델 지정 (기본: `deepseek-flash`) |
| `--max-steps <N>` | 에이전트 루프 최대 반복 (기본: 40) |
| `--no-stream` | 스트리밍 비활성화 |
| `--no-thinking` | 사고(thinking) 비활성화 (더 빠른 응답, `--chat`에서 유용) |
| `--show-thinking` | 사고 과정 원문을 회색으로 실시간 출력 (기본은 글자 수만 표시) |
| `--confirm-all` | 읽기 전용 명령도 확인 후 실행 (안전 명령 자동 실행 비활성) |
| `--allow-list` | 이 세션의 셸 자동 허용 목록 보기 |
| `--allow-clear` | 이 세션의 셸 자동 허용 목록 비우기 |
| `--init-rules` | 이 폴더에 `.llm/rules.md` 생성 |
| `--rules` | 이 폴더의 세션 규칙 보기 |
| `--summarize` | 현재 대화를 지금 요약·백업 (강제 실행) |
| `--run` | 직전 응답의 bash/sh 코드 블록 추출 후 실행 |
| `--view` | 세션 대화 내용 보기 (Markdown) |
| `--clear` | 현재 세션 내용만 비우기 |
| `--delete` | 현재 세션 파일 삭제 및 폴더 매핑 해제 |
| `--balance` | DeepSeek API 잔액 조회 (KRW 환산) |

### 사용 예

```bash
# 다른 폴더를 작업 루트로 지정
llm --root ~/project/src "타입 힌트가 빠진 함수를 찾아줘"

# 세션 이름을 명시적으로 고정
llm -s mysession "이 대화는 mysession으로 저장해줘"

# 최근 답변의 스크립트를 추출해 실행
llm --run

# 세션 초기화 / 삭제
llm --clear
llm --delete
```

> 옵션과 프롬프트를 함께 쓸 때는 프롬프트를 `--`로 구분할 수 있습니다.
> 예: `llm --root . -- "질문"`

---

## 세션과 폴더 매핑

- 하나의 **세션 이름**은 하나의 **폴더**에만 연결됩니다. 대화 기록은
  `~/.local/share/llm/history_<세션>.json`에 저장됩니다.
- 폴더-세션 매핑은 `~/.local/share/llm/bindings.json`에 저장됩니다.
- 매핑이 없는 폴더에서 대화를 시작하면 **폴더 이름 기반으로 세션이 자동
  생성**됩니다. (예: `~/work/api` → 세션 `api`; 이름 충돌 시 해시 접미사 추가)
- `-s <이름>`으로 세션을 옮길 수 있고, 이전 폴더 매핑이 있으면 어디서
  옮겨왔는지 안내합니다.
- `--view/--clear/--delete` 같은 조회·삭제 명령은 다른 세션을 가로채지
  않습니다.

현재 세션 이름은 `~/.local/share/llm/current_session`에 마지막 값이
기록됩니다.

---

## 대화 요약과 백업

대화가 길어지면 컨텍스트를 줄이기 위해 **자동 요약**되고, 요약 시점의
원본도 함께 백업됩니다.

- 트리거(하이브리드)는 **압축 대상**, 즉 최근 `SUMMARY_KEEP_RECENT`(24)개를
  제외한 앞부분만 보고 판단합니다. 그 앞부분이 문자 수
  `SUMMARY_TRIGGER_CHARS`(120,000) 초과 **또는** 메시지 수
  `SUMMARY_TRIGGER_MESSAGES`(160) 초과 시 자동 실행.
  - 최근 메시지는 어차피 유지되므로, 대용량 도구 출력 몇 개로 요약이 걸리지
    않습니다.
- **쿨다운**: 직전 요약 이후 새 메시지가 `SUMMARY_MIN_NEW_MESSAGES`(40)개
  미만이면 요약하지 않습니다. 요약 직후 재요약이 겹치는 것을 막습니다.
  - 쿨다운은 요약본에 남긴 "요약 시점 메시지 수" 표시(HTML 주석)로 판단하며
    화면 표시에는 영향이 없습니다.
- 요약 후 최근 `SUMMARY_KEEP_RECENT`(24)개 메시지만 남기고, 이전 내용은
  요약문으로 대체합니다.
- 요약문은 최대 `SUMMARY_MAX_CHARS`(4,000)자, 한국어로 생성됩니다.
- 수동 실행: `/summary`, `/summarize`, `/요약` 슬래시 명령 또는
  `llm --summarize`(강제).
- 백업(요약본 `.md` + 원본 스냅샷 `.json`)은
  `~/.local/share/llm/summaries/<세션>/`에 저장됩니다.

---

## 응답 표시와 사고 과정

기본은 **진행 과정만 보여주는 출력**입니다. 화면에는 아래 세 가지만 남습니다.

- **단계 구분선**: 에이전트 모드에서 매 반복마다 `── 3/40단계 ──`처럼 현재
  단계를 한 줄로 표시합니다.
- **도구 호출 한 줄**: `🔧 shell 파일을 작성하거나 수정합니다` 처럼
  **무엇을 하려는지 의도**를 먼저 표시하고(파일 도구는 경로), 바로 아래 줄에
  `✓`/`✗`와 결과 첫 줄을 붙입니다. 명령 원문은 확인이 필요할 때만 프롬프트에
  나옵니다.
- **진행 상태 한 줄**: 모델이 응답을 만드는 동안 `🧠 사고 중… 1,234자`,
  `✍️ 작성 중… 567자`처럼 글자 수만 갱신합니다.

- **사고 과정(thinking)**: 모델이 `reasoning_content`를 반환해도 **원문은 기본
  으로 출력하지 않습니다**(영어 사고 과정이 화면을 채우는 것을 막기 위함).
  원문을 보고 싶으면 `--show-thinking`을 쓰면 회색으로 실시간 출력합니다.
  아예 사고를 끄려면 `--no-thinking`.
- **최종 답변**: 완료 후 `glow`(있으면)로 Markdown 렌더링하고, TTY가
  아니거나 `LLM_RAW` 설정 시 원문으로 출력합니다. 스트리밍 중에는 글자 수만
  표시합니다.
- **비스트리밍/폴백**: `--no-stream`이거나 스트림이 실패하면 완료 후 결과를
  한 번에 출력합니다. 이때도 사고 과정 원문은 감추고 `사고 과정 N자 진행`
  안내만 보여줍니다(`--show-thinking`이면 원문 출력).
- 사고 과정은 API 프로토콜상 같은 턴에서만 유효하므로 디스크에 저장하지
  않습니다.

---

## 세션 규칙

프로젝트별 지침을 대화 기록과 별개로 코드 저장소에 남길 수 있습니다.
규칙은 **매 API 요청의 시스템 프롬프트에 포함**되어 모델이 따릅니다.

### 파일 위치

```
<작업폴더>/.llm/rules.md        # 기본 규칙 파일
<작업폴더>/.llm/rules/*.md      # 추가 규칙 (파일명 사전순으로 병합)
```

### 사용

```bash
# 템플릿 생성
llm --init-rules

# 현재 적용되는 규칙 확인
llm --rules
```

- 두 위치의 규칙을 합쳐 최대 `MAX_RULES_CHARS`(24,000자)까지 적용합니다.
- 규칙이 없으면 모델은 규칙 없이 동작합니다.
- "규칙을 수정해줘"라고 요청하면 에이전트가 해당 파일을 직접 편집합니다.

### 작성 예

```markdown
# 세션 규칙

## 프로젝트
- Python 3.10, 표준 라이브러리만 사용
- 패키지 구조는 src/ 아래 유지

## 코딩 규칙
- 편집은 str_replace 사용, 최소 변경
- 한글 응답

## 하지 말 것
- 의존성 추가 금지
- .env, 키 파일 출력 금지

## README
- 기능/옵션 변경 시 README.md를 즉시 갱신
```

> **이 저장소의 규칙**: `agent.py`의 기능, CLI 옵션, 동작이 바뀌면
> `README.md`를 **즉시** 갱신합니다. 자세한 내용은 `.llm/rules.md` 참고.

---

## 에이전트 도구

에이전트 모드에서 모델이 사용할 수 있는 도구입니다. 모든 경로는 **작업
루트(workspace) 안으로 제한**됩니다.

| 도구 | 설명 | 주요 인자 |
|------|------|-----------|
| `read_file` | 텍스트 파일 읽기 (1-based 줄 번호 prefix) | `path`, `offset`, `limit` |
| `write_file` | 파일 생성/덮어쓰기 | `path`, `contents` |
| `str_replace` | 고유 문자열 치환 (기존 파일 편집) | `path`, `old_string`, `new_string`, `replace_all` |
| `delete_file` | 워크스페이스 내 파일 삭제 | `path` |
| `list_dir` | 디렉터리 목록 | `path` |
| `glob` | 패턴으로 파일 찾기 (예: `**/*.py`) | `pattern`, `path` |
| `grep` | 정규식으로 내용 검색 | `pattern`, `path`, `glob`, `case_insensitive` |
| `shell` | bash 명령 실행 | `command`, `timeout` |

### 제약

- 경로는 워크스페이스 밖으로 나갈 수 없습니다.
- `.env`, `credentials.json`, `id_rsa`, `*.pem`, `*.key`, `.ssh/*` 등
  비밀/자격 증명 파일은 도구로 접근할 수 없습니다.
- 읽기/도구 출력은 각각 `MAX_READ_BYTES`(200KB), `MAX_TOOL_CHARS`(80KB),
  grep은 `MAX_GREP_HITS`(80)로 제한됩니다. 큰 파일은 offset/limit을 사용합니다.

---

## 셸 실행과 안전장치

`shell` 도구는 다음 안전장치를 가집니다.

1. **위험 패턴 차단** — 실행 전 금지 패턴 검사
   (예: `rm -rf /`, `mkfs.*`, `dd of=/dev/*`, fork bomb, `shutdown/reboot` 등)
2. **읽기 전용/검증 명령 자동 실행** — 아래 화이트리스트에 해당하면 확인 없이
   바로 실행합니다(실행 전에 무엇을 하는지 한 줄로만 안내).
   - 조회 계열: `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `find`, `stat`,
     `pwd`, `echo`, `env`, `which`, `file`, `du`, `df`, `date`, `cd`, `sleep` 등
   - 조회 계열 2단어 조합: `pip list`, `pip show`, `node -v`, `rustc --version`,
     `tsc --noEmit`, `java -version` 등 (`SHELL_SAFE_PAIRS`)
   - 서브커맨드 조회/검증: `npm ls`, `npm test`, `cargo test`, `cargo check`,
     `go build`, `docker ps`, `kubectl get`, `systemctl status` 등
     (`SHELL_SAFE_SUBCOMMANDS`)
   - 테스트/린트/타입 검사: `pytest`, `ruff check .`, `flake8`, `mypy`, `eslint`,
     `shellcheck`, `gofmt`, `black --check`, `prettier --check`
     (`--fix`/`--write`/`-w`처럼 파일을 고치는 플래그가 있으면 확인 필요,
     `SHELL_SAFE_CHECKERS`/`SHELL_CHECK_WRITE_FLAGS`)
   - 프로젝트 스크립트: `npm test`, `npm run <test|lint|build|typecheck|...>`,
     `make <test|build|check|lint|...>` (`SHELL_SAFE_RUN_SCRIPTS`/`SHELL_SAFE_MAKE_TARGETS`)
   - Git 조회·스테이징: `git status`, `git log`, `git diff`, `git show`,
     `git branch`, `git add` (index만 변경, `git reset`으로 되돌림)
     — `commit`/`push`/`pull`/`checkout`/`reset`/`clean`/`stash`는 확인 필요
   - Python 조회: `python3 -m py_compile`, `python3 -m json.tool`, `python3 -m unittest`,
     `python3 script.py`, `python3 --version` 등 (`-c`/`-i` 같은 임의 코드 실행은 확인 필요)
   - 버전/도움말: `git --version`, `cargo --version`, `docker --version` 등
     (경로 실행 `./run.sh --version`은 제외)
   - 연결 명령(`&&`, `|`): **모든 조각이 자동 실행 대상일 때만** 자동 실행합니다.
     예) `git add -A && pytest -q`, `ls -la | head -5`는 자동 실행,
     `ls && rm -rf x`, `pytest -q && npm install`은 확인 대상.
     `;`(순차 연결)은 항상 확인 대상입니다.
   - 리다이렉션은 위험 판정(`> out.txt` 등), `/dev/null`·`2>&1`은 무해 처리
   - `--confirm-all`로 자동 실행을 끌 수 있습니다.
3. **사용자 확인** — 자동 실행 대상이 아니면 TTY에서 `[y/N/a]` 확인을 받습니다.
   프롬프트에는 **실제로 실행될 명령**만 보여줍니다(무엇을 하려는지는 위의
   `🔧 shell <의도 요약>` 줄에 이미 표시됩니다).
   - `&&`, `||`, `|`, `;`로 이어진 조합 명령은 한 덩어리로 묻지 않고 **조각
     단위로 하나씩** 묻습니다(`1)`, `2)` …). 각 조각에 `y`(이번만) / `a`(그
     조각 자동 허용) / `n`(전체 중단), 남은 조각을 한꺼번에 허용하려면 `all`.
4. **자동 허용 목록(`a`)** — 확인 프롬프트에서 `a`를 고르면 같은 명령을
   **다시 묻지 않고** 실행합니다. `--yes`와 달리 명령별로만 허용되며
   세션별 파일에 저장됩니다.
   - 저장 범위: 위험하지 않은 명령은 **"계열"**(`npm install` 등 앞 두 토큰)로
     저장해 `npm install -g x`, `npm install --save y`를 함께 허용하고, 정확히
     한 단어뿐이거나 서브커맨드가 없는 명령(`pytest`, `make` 등)은 정확
     일치로 저장합니다.
   - **조각별 저장**: 조합 명령은 조각마다 따로 기억합니다. `npm run build &&
     docker build -t app .`의 두 조각을 각각 `a`로 허용하면, 순서를 바꾸거나
     인자가 달라져도(`docker build -t app:v2 . && npm run build`) 다시 묻지
     않습니다. `n`으로 중단해도 그 전에 `a`를 고른 조각은 허용 목록에 남습니다.
   - 허용 제외: `rm`, `sudo`, `curl`, `dd`, `chmod` 등 위험 프로그램은 계열
     허용하지 않습니다(정확 일치만). 조각 안에 리다이렉션(`>`), 백그라운드
     (`&`), 명령 치환(`$()`, 백틱)이 남아 있으면 계열 허용하지 않습니다.
     프롬프트에는 계열 허용이면 `a = 이 명령/<접두> 계열 자동 허용`,
     정확 일치면 `a = 이 명령만 자동 허용`으로 표시됩니다.
   - 인자에 따라 쓰기로 바뀌는 명령도 계열 허용하지 않습니다. 검사 도구의
     `--fix`/`-w`/`--install-types`, `find -exec`/`-delete`, `--in-place`,
     `--output`처럼 부작용을 켜는 인자가 붙으면 매번 확인합니다
     (예: `ruff check .`을 허용해도 `ruff format .`은 확인 대상).
   - 목록 확인/해제: `llm --allow-list`, `llm --allow-clear`
5. **TTY가 아니면** `--yes` 없이 실행 불가
6. **타임아웃** — 기본 60초, 최대 180초

### 확인 프롬프트 예시

도구 호출 줄에서 무엇을 하려는지 먼저 요약해 보여주고, 확인이 필요한 경우에만
프롬프트가 뜹니다. 조합 명령은 조각 단위로 하나씩 묻습니다.

```
🔧 shell 빌드를 실행합니다 → 컨테이너를 다룹니다
❓ 확인이 필요합니다 (조각 2개 중 2개 확인)
   전체: npm run build && docker build -t app .
   1) 실행할 명령: npm run build
   a = 이 명령/npm run 계열 자동 허용 (다시 묻지 않음)
   [y/N/all/a]: a
   2) 실행할 명령: docker build -t app .
   a = 이 명령/docker build 계열 자동 허용 (다시 묻지 않음)
   [y/N/all/a]: a
✅ 자동 허용에 추가했습니다: npm run, docker build (해제: llm --allow-clear)
```

단일 명령이면 이전처럼 한 번만 묻습니다(`[y/N/a]`, `all` 없음). 조각마다 `a`를
고르면 조각별로 저장되므로 **조합 순서나 인자가 바뀌어도** 이미 허용한 조각은
다시 묻지 않습니다(예: `docker build -t app:v2 . && npm run build`는 바로 자동
통과). 위험 프로그램(`rm`, `curl` 등)과 리다이렉션·명령 치환이 섞인 조각은
정확히 같은 명령일 때만 자동 통과합니다. 그래서 `cd ... && cat > f <<'PY'`
처럼 내용이 매번 달라지는 조합은 `a`로 허용해도 다음에는 다시 묻습니다. 이런
반복 작업이 많으면 `--auto-workspace`(또는 `LLM_AUTO_WORKSPACE=1`)를 쓰세요.

### 작업 폴더 안 자동 실행 (`--auto-workspace`)

`--auto-workspace`를 켜면 **명령이 건드리는 경로가 전부 작업 폴더 안에서
끝나고, 되돌릴 수 있는 명령이면** 확인 없이 실행합니다. "폴더 안이면 다
허용"에 가깝지만 아래 조건으로 범위를 막습니다.

- 자동 실행되는 예: `rm -rf build`, `mkdir -p src/new`, `mv a.py b.py`,
  `sed -i s/a/b/ main.py`, `echo hi > out.txt`, `ruff format .`, `black .`,
  `npm test > out.txt`, `git add -A`, `git commit -m wip`, `git tag v1`,
  `find . -name '*.py'`, `make test > log.txt`
- 그래도 확인하는 경우:
  - **작업 폴더 밖을 건드림**: `rm -rf ../x`, `cp a.py /tmp/x`, `> /etc/hosts`,
    `~/...`, `/opt`, 변수·명령 치환(`$VAR`, `` `...` ``)으로 경로를 숨긴 명령
  - **설치·네트워크·권한 상승**: `sudo`, `apt`/`pip install`/`npm install`,
    `curl`/`wget`/`git clone`, `docker`/`kubectl`/`systemctl`, `kill`/`pkill`
  - **되돌릴 수 없는 Git 작업**: `push`, `pull`, `fetch`, `checkout`, `switch`,
    `restore`, `reset`, `clean`, `stash`, `rm`, `filter-branch`, `--amend`,
    `--force`, `--hard`, `--global`
  - **`.git`/`.`/`*` 삭제·이동**: `rm -rf .git`, `rm -rf .`, `rm -rf *`,
    `mv .git x`
  - **임의 코드 실행**: `bash run.sh`, `sh -c`, `./run.sh`, `python3 -c`,
    `node -e`, 인터프리터 단독 실행, 스크립트 파일 직접 실행
  - **쓰기 옵션을 켠 `find`**: `find . -exec rm {} +`, `find . -delete`,
    `find . -ok ...`
- `--confirm-all`과 함께 쓰면 `--auto-workspace`는 무시됩니다(모두 확인).
- 매번 플래그를 치기 번거로우면 `/etc/env/.env`에 `LLM_AUTO_WORKSPACE=1`을
  넣어 기본으로 켜 둘 수 있습니다(`--confirm-all`이 항상 우선).
- `--yes`처럼 확인 자체를 없애지는 않으므로, 위험한 명령은 그대로
  `[y/N/a]` 프롬프트를 거칩니다.

### 반드시 확인이 필요한 명령

아래는 자동 실행하지 않고 항상 `[y/N/a]` 확인을 받습니다(필요하면 `a`로
세션 한정 자동 허용).

| 분류 | 예 | 이유 |
|------|-----|------|
| 삭제·덮어쓰기·권한 | `rm`, `rmdir`, `mv`, `cp`, `truncate`, `dd`, `shred`, `chmod`, `chown`, `tee` | 되돌리기 어렵거나 데이터 손실 |
| 다운로드·설치·전송 | `curl`, `wget`, `pip install`, `npm install`, `npm ci`, `uv pip install`, `poetry install`, `apt install` | 외부 통신, 환경 변경 |
| 원격·이력 변경 | `git commit`, `git push`, `git pull`, `git fetch`, `git checkout`, `git switch`, `git reset`, `git clean`, `git stash` | 이력/원격 상태 변경 |
| 임의 코드 실행 | `bash run.sh`, `sh -c`, `./run.sh`, `python3 -c`, `eval`, `sed -i`, `awk`, `xargs` | 무엇이든 실행될 수 있음 |
| 서비스·컨테이너 변경 | `docker run/build`, `kubectl apply/delete`, `systemctl restart`, `kill`, `pkill` | 실행 중인 환경에 영향 |
| 파일을 고치는 검사 | `ruff check --fix`, `eslint --fix`, `black .`, `cargo fmt` | 검사 도구지만 파일 수정 |
| 위험 조각이 섞인 연결 | `ls && rm -rf x`, `pytest -q && npm install`, `ls; cat f`(`;`) | 한 조각이라도 위험하면 그 조각은 확인 |

그 밖의 명령(예: `make install`, `make clean`, `npm run deploy`)도 기본은
확인 대상이며, 조각마다 `y`(이번만) / `a`(그 조각 자동 허용) / `n`(전체 거부)
중에서 고릅니다(조합 명령이면 `all`로 남은 조각을 한꺼번에 허용).
`llm --yes`는 모든 확인을 건너뜁니다.

---

## 설정과 환경변수

| 이름 | 기본값 | 설명 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | (없음) | **필수**. API 키 |
| `LLM_MODEL` | `deepseek-flash` | 기본 모델 |
| `LLM_RAW` | (미설정) | 설정 시 Markdown 렌더링(glow)을 끄고 원문 출력 |
| `LLM_AUTO_WORKSPACE` | (미설정) | `1`/`true`/`yes`/`on` 이면 항상 `--auto-workspace` 켜짐 |
| `NO_COLOR` | (미설정) | 설정 시 색상 출력 비활성화 |

`/etc/env/.env` 파일이 있으면 시작 시 로드됩니다(이미 설정된 환경변수는
덮어쓰지 않음).

---

## 파일/데이터 위치

| 경로 | 용도 |
|------|------|
| `/etc/env/.env` | API 키 등 환경변수 (자동 로드) |
| `~/.local/share/llm/history_<세션>.json` | 세션별 대화 기록 |
| `~/.local/share/llm/bindings.json` | 폴더 ↔ 세션 매핑 |
| `~/.local/share/llm/current_session` | 마지막 사용 세션 이름 |
| `~/.local/share/llm/summaries/<세션>/` | 요약본(`.md`)과 원본 스냅샷(`.json`) 백업 |
| `~/.local/share/llm/shell_allow_<세션>.json` | 세션별 셸 자동 허용 목록 (`a` 응답) |
| `<작업폴더>/.llm/rules.md` | 프로젝트 세션 규칙 |

대화 기록 저장은 임시 파일에 쓴 뒤 `replace`하는 방식으로 원자적으로
처리됩니다. 중간에 끊긴(도구 호출만 있고 결과가 없는) 턴은 로드 시 자동
정리됩니다. 사고 과정(`reasoning_content`)은 API 프로토콜상 같은 턴에서만
되돌려주고, 디스크에는 저장하지 않습니다(`strip_reasoning`).

---

## 아키텍처

```
llm (bash)  →  agent.py (python)
```

`agent.py`의 주요 구성:

- **진입/CLI**: `main`, `parse_args`, `resolve_root`
- **에이전트 루프**: `cmd_agent` — 모델 호출 → 도구 실행 → 결과 피드백 반복
- **API 계층**: `http_json`, `stream_chat`, `complete` (SSE 스트리밍, 비스트림
  폴백, `StreamInterrupted`로 부분 응답 보존, `on_delta`/`make_stream_progress`
  로 사고·본문 진행 표시)
- **도구 계층**: `tool_*` 함수 + `run_tool` 디스패처 + `tool_schemas`
- **안전/검증**: `resolve_in_workspace`, `is_secret_path`, `SHELL_DENY`,
  `is_safe_command`(읽기 전용 자동 실행), `intent_summary`(명령 의도 요약),
  `confirm_shell` / `ask_shell_segment`(조각별 `[y/N/a]`/`all`),
  `ShellApproval`(세션별 자동 허용 목록, `--allow-list`/`--allow-clear`)
- **세션/규칙**: `load_history`/`save_history`, `bind_folder`/`load_bindings`,
  `load_session_rules`
- **요약/백업**: `should_summarize`, `summarize_messages`, `backup_conversation`,
  `apply_summary`, `maybe_summarize`, `cmd_summarize`
- **UI**: `render_markdown`(glow), `print_title`/`print_info`/`print_ok`/
  `print_warn`/`print_error`, `rule`(구분선, 한글 폭 고려)

### 처리 흐름 (에이전트 모드)

1. 사용자 프롬프트를 세션 기록에 추가하고 저장
2. 시스템 프롬프트(워크스페이스 정보 + 세션 규칙) 구성
3. `── N/M단계 ──` 구분선 출력 후 모델 호출 → (사고/작성 진행 상태 한 줄) →
   텍스트 또는 도구 호출 수신
4. 도구 호출이면 `🔧 이름 <의도 요약>` 한 줄 + `✓/✗ 결과 요약`을 출력하고
   실행 후 결과를 대화에 추가해 3으로 반복 (셸은 확인이 필요할 때만 명령 원문
   프롬프트)
5. 최종 텍스트 응답을 저장하고 Markdown으로 출력

---

## 문제 해결

### `llm: command not found`

- `~/bin`이 PATH에 없을 수 있습니다.
  ```bash
  echo $PATH | tr ':' '\n' | grep "$HOME/bin"
  echo 'export PATH="$HOME/bin:$PATH"' >> ~/.bashrc
  source ~/.bashrc
  ```
- 심볼릭 링크 확인:
  ```bash
  readlink ~/bin/llm   # agent.py가 있는 llm을 가리켜야 함
  ```

### `DEEPSEEK_API_KEY가 없습니다`

- `/etc/env/.env` 또는 환경변수에 키가 있는지 확인하세요.

### 답변이 Markdown 원문으로 보임

- `glow`가 없거나, TTY가 아니거나, `LLM_RAW`가 설정된 경우입니다.
  ```bash
  which glow
  ```

### 셸 실행이 거부됨

- TTY가 아닌 환경이면 `--yes`가 필요합니다.

---

## 개발 규칙

이 저장소에서 작업할 때 지켜야 할 규칙입니다.

1. **표준 라이브러리만** 사용합니다(의존성 추가 금지).
2. 변경은 **최소**로, 기존 구조와 UI 스타일(아이콘/색상/구분선)을 유지합니다.
3. 기존 파일 편집은 `str_replace`로, 새 파일은 `write_file`로 작성합니다.
4. **기능/CLI 옵션/동작이 바뀌면 `README.md`를 즉시 갱신합니다.**
5. 한글 응답을 기본으로 합니다.
6. `.env`, 키 파일 등 비밀 정보는 출력하지 않습니다.
7. 편집 후에는 가능한 범위에서 검증합니다(문법 검사, 실행 확인 등).

세션 규칙 원본은 [`.llm/rules.md`](.llm/rules.md)에서 관리됩니다.

---

## 라이선스

별도 라이선스가 명시되지 않은 개인용 프로젝트입니다.
