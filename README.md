# llm — DeepSeek CLI 에이전트

터미널에서 DeepSeek API를 사용해 파일 읽기/편집, 검색, 셸 실행을 수행하는
경량 코딩 에이전트입니다. Python 표준 라이브러리만으로 동작하며, 별도의
패키지 설치가 필요 없습니다.

- **에이전트 모드**: 도구(파일/검색/셸)를 스스로 호출하며 작업을 완수
- **채팅 모드**: 도구 없이 순수 대화
- **세션**: 폴더별로 대화 기록을 자동 연결·보존
- **세션 규칙**: 프로젝트별 지침을 `.llm/rules.md`에 남겨 매 요청에 반영
- **사고 과정 표시**: 모델의 thinking을 회색으로 실시간 출력
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

# 사고 과정 없이 빠르게 답만 받기
llm --no-thinking --chat "리스트와 튜플 차이는?"

# 읽기 전용 명령도 매번 확인 (자동 실행 비활성)
llm --confirm-all "이 폴더 상태를 점검해줘"
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
| `--root <경로>` | 작업 디렉터리 지정 (기본: 현재 폴더) |
| `--model <이름>` | 모델 지정 (기본: `deepseek-flash`) |
| `--max-steps <N>` | 에이전트 루프 최대 반복 (기본: 40) |
| `--no-stream` | 스트리밍 비활성화 |
| `--no-thinking` | 사고(thinking) 비활성화 (더 빠른 응답, `--chat`에서 유용) |
| `--confirm-all` | 읽기 전용 명령도 확인 후 실행 (안전 명령 자동 실행 비활성) |
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

- **사고 과정(thinking)**: 모델이 `reasoning_content`를 반환하면 회색으로
  실시간 스트리밍 출력합니다(터미널 폭에 맞춰 줄바꿈, 블록 종료 시 정리).
  `--no-thinking`으로 끌 수 있습니다.
- **최종 답변**: 완료 후 `glow`(있으면)로 Markdown 렌더링하고, TTY가
  아니거나 `LLM_RAW` 설정 시 원문으로 출력합니다. 스트리밍 중에는 글자 수만
  표시합니다.
- **비스트리밍/폴백**: `--no-stream`이거나 스트림이 실패하면 완료 후 사고
  과정과 본문을 한 번에 출력합니다.
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
2. **읽기 전용 명령 자동 실행** — 아래 화이트리스트에 해당하면 확인 없이
   바로 실행하고 `ℹ️  가벼운 명령은 확인 없이 실행합니다: ...`를 안내합니다.
   - 조회 계열: `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `find`, `stat`,
     `pwd`, `echo`, `env`, `which`, `file`, `du`, `df`, `date` 등
   - Git 조회: `git status`, `git log`, `git diff`, `git show`, `git branch` 등
     (조회 계열만; `commit`/`push`/`checkout` 등은 확인 필요)
   - Python 조회: `python3 -m py_compile`, `python3 -m json.tool`, `python3 -m unittest`,
     `python3 script.py` 등 (`-c`/`-i` 같은 임의 코드 실행은 확인 필요)
   - 리다이렉션은 위험 판정(`> out.txt` 등), `/dev/null`·`2>&1`은 무해 처리
   - `--confirm-all`로 자동 실행을 끌 수 있습니다.
3. **사용자 확인** — 자동 실행 대상이 아니면 TTY에서 실행 전에 명령을 요약해
   보여주고 `[y/N]` 확인
   - 단일 명령: 요약 + 실제 명령
   - 다중 명령: `;`, `&&`, `||`, `|`로 분해해 **실행 순서와 연결 의미**를 표시
     (성공 시 / 실패 시 / 파이프 / 순차)
4. **TTY가 아니면** `--yes` 없이 실행 불가
5. **타임아웃** — 기본 60초, 최대 180초

### 확인 프롬프트 예시

```
❓ 이 명령을 실행할까요?
   • 2개 명령을 다음 순서로 실행합니다:
     1)  git — Git 작업
        git add -A
     2) → (성공 시) git — Git 작업
        git commit -m "save"
   ── 실행할 명령 ──
   git add -A && git commit -m "save"
   [y/N]:
```

---

## 설정과 환경변수

| 이름 | 기본값 | 설명 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | (없음) | **필수**. API 키 |
| `LLM_MODEL` | `deepseek-flash` | 기본 모델 |
| `LLM_RAW` | (미설정) | 설정 시 Markdown 렌더링(glow)을 끄고 원문 출력 |
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
  `is_safe_command`(읽기 전용 자동 실행), `confirm_shell`
- **세션/규칙**: `load_history`/`save_history`, `bind_folder`/`load_bindings`,
  `load_session_rules`
- **요약/백업**: `should_summarize`, `summarize_messages`, `backup_conversation`,
  `apply_summary`, `maybe_summarize`, `cmd_summarize`
- **UI**: `render_markdown`(glow), `print_title`/`print_info`/`print_ok`/
  `print_warn`/`print_error`, `rule`(구분선, 한글 폭 고려)

### 처리 흐름 (에이전트 모드)

1. 사용자 프롬프트를 세션 기록에 추가하고 저장
2. 시스템 프롬프트(워크스페이스 정보 + 세션 규칙) 구성
3. 모델 호출 → (사고 과정 진행 표시) → 텍스트 또는 도구 호출 수신
4. 도구 호출이면 실행 후 결과를 대화에 추가하고 3으로 반복
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
