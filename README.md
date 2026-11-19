# DeepSeek CLI — `llm`

로컬 PC에서 사용하는 가벼운 Python 기반 코딩 에이전트. 대화 세션·요약·사용자 프롬프트는 **로컬 전용**이며 GitHub에는 프로그램 소스와 공개 가능한 프로젝트 규칙만 보관합니다.

**버전:** v0.14.0 — 간결한 CLI, 로컬 세션·프롬프트 관리, 비용·권한 최적화

## 1. 최초 설치 — Clone 후 한 번만

필수: **Python 3.10 이상**, Git. 외부 Python 패키지는 필요하지 않습니다.

### Ubuntu / Linux

```bash
git clone https://github.com/hanurijang/llm.git
cd llm
bash install.sh
```

설치 스크립트는 `~/.local/bin/llm` 심볼릭 링크를 만들고 `~/.profile` 및 `~/.bashrc`에 PATH를 등록합니다. **sudo는 필요 없습니다.** 새 터미널을 열면 어디서든 `llm`을 실행할 수 있습니다. 현재 터미널에서 바로 사용하려면 다음을 실행하세요.

```bash
export PATH="$HOME/.local/bin:$PATH"
llm help
```

### Windows (PowerShell)

```powershell
git clone https://github.com/hanurijang/llm.git
cd llm
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

사용자 계정의 `%LOCALAPPDATA%\Programs\llm\bin\llm.cmd`를 생성하고 **사용자 PATH**에 등록합니다. 관리자 권한은 필요하지 않습니다. 새 PowerShell 또는 CMD 창을 열어 `llm help`로 확인하세요. **코딩 에이전트의 셸 명령·백그라운드 작업을 사용하려면 Git Bash(Git for Windows)가 PATH에서 `bash`로 검색되어야 합니다.** 기본 도움말, 잔액 조회 및 대화 실행은 Python으로 실행됩니다. Windows PowerShell 전용 셸 스크립트를 자동으로 실행하는 방식은 지원하지 않습니다.

> **저장소 폴더를 이동하거나 삭제하면 등록된 명령이 작동하지 않습니다.** 이동한 경우 해당 저장소에서 설치 스크립트를 다시 실행하세요. 재실행은 안전하며 등록 경로를 갱신합니다.

## 2. API와 데이터 저장 설정

Clone한 `llm` 폴더 안에 **Git에서 제외되는** `.env` 파일을 직접 만드세요.

```dotenv
DEEPSEEK_API_KEY=발급받은_API_키
# 빈 값이면 각 작업 폴더의 .llm/에 로컬 저장
LLM_HISTORY_DIR=
# 별도 데이터 드라이브(예: Ubuntu)
# LLM_HISTORY_DIR=/data/llm-sessions
```

`LLM_HISTORY_DIR`는 절대경로를 권장합니다. 상대경로라면 **명령을 실행한 작업 폴더 기준**입니다. 기존 기록을 다른 위치로 옮길 때는 먼저 직접 백업·이전하세요. `.env`, 대화 기록, 로그, 세션 프롬프트는 GitHub에 올리지 않습니다.

## 3. 명령어 — 다섯 가지 형태

| 명령 | 동작 |
|---|---|
| `llm` | 현재 폴더의 세션으로 REPL 대화 시작 |
| `llm "질문"` | 질문·작업을 **한 번 실행한 후 종료** |
| `llm bal` | DeepSeek API 잔액 조회 |
| `llm help` | 간단한 사용법 |
| `llm session` | 로컬 세션 관리 메뉴 |

**현재 작업 폴더가 곧 프로젝트 기준**입니다. 실행 위치별로 세션을 자동 연결하고, 다른 폴더에 소유권이 지정된 세션을 임의로 변경하지 않습니다. 터미널이 아닌 환경에서 인자 없이 실행하면 도움말을 표시합니다.

### REPL 내부 명령

| 입력 | 동작 |
|---|---|
| `/agent` | 에이전트 모드(파일 분석·수정·도구 실행) |
| `/chat` | 일반 대화 모드 |
| `/plan` | 읽기 전용 분석·계획 모드 |
| `/undo` | 프로그램이 추적한 직전 파일 편집 복원 |
| `/bal` | 잔액 조회 |
| `/prompt` | 프롬프트 편집 메뉴 |
| `/help` | 내부 명령 안내 |
| `/exit` | 종료 |

모드 전환 시 같은 세션의 맥락을 유지합니다. `/undo`는 모든 셸 명령·외부 파일 변경을 되돌리는 기능은 아닙니다. 기존 세부 플래그·명령어 중 일부는 호환성 목적으로 남아 있으나 기본 도움말에서는 숨깁니다. `llm sync`를 통한 대화 기록 동기화는 **사용할 수 없습니다**.

## 4. 프롬프트 관리 — `/prompt`

REPL에서 `/prompt` → `1 System` / `2 Project` / `3 Session`을 고른 뒤, `E`로 편집, `V`로 조회, `D`로 삭제할 수 있습니다. 편집기는 `EDITOR` 환경변수나 `nano`/`vi`를 사용합니다.

| 계층 | 저장 위치 | 영향 범위 |
|---|---|---|
| 시스템 | `~/.local/share/llm/system.md` | 이 PC 전체 |
| 프로젝트 | `<작업 폴더>/.llm/rules.md` | 현재 프로젝트 |
| 세션 | `<기록 저장 폴더>/prompts/<세션명>.md` | 현재 세션 |

**시스템·세션 프롬프트는 로컬 전용입니다.** 프로젝트 규칙 `.llm/rules.md`는 Git에서 관리할 수 있으므로 토큰·비밀번호·개인정보를 넣지 마세요. 애플리케이션의 기본 도구 보호 정책은 사용자 프롬프트로 해제되지 않습니다.

## 5. 세션 메뉴 — `llm session`

한 화면에서 **세션 목록·대화 조회·현재 폴더에서 사용할 세션 선택·대화 초기화·세션 삭제**를 관리합니다. 초기화·삭제는 이름을 다시 입력해 확인합니다. 삭제 시 해당 세션의 프롬프트도 삭제됩니다. 자동 생성된 세션은 기본적으로 별도 설정이 필요 없습니다.

## 6. REPL 권한과 토큰 비용

- 기본값은 `safe`입니다. 안전한 조회·테스트·작업 폴더 내 작업은 자동 실행하며 설치, 권한 상승, 위험한 명령은 확인을 받습니다.
- `sudo` 등의 관리자 권한은 별도로 승인하며, 비밀정보로 보이는 셸 명령은 별도 보호합니다. 세션에 기록된 권한 허용 내역은 이 PC에서만 사용합니다.
- 오래된 도구 출력부터 정리한 뒤 필요한 경우에만 유료 API 요약을 요청합니다. 최근 대화와 도구 호출의 연결 구조는 보존하며 요약 직후 불필요한 재요약을 억제합니다.
- `llm bal` 또는 REPL `/bal`로 현재 잔액을 확인합니다. 실제 토큰 및 요약 비용 절감률은 이용 기록으로 따로 평가해야 합니다.

## 7. 코드 변경·테스트

```bash
cd /path/to/llm
git pull origin main
python3 -m unittest discover -s tests -v
```

Windows에서는 `python3` 대신 `py -3` 또는 `python`을 사용합니다. CI는 GitHub Actions에서 Python 문법과 단위 테스트를 실행합니다. UI의 대화형 편집기·실제 DeepSeek API 연결은 별도의 수동 확인이 필요합니다.

## v0.14 변경 사항

현재 기준의 주요 기능은 로컬 전용 세션·프롬프트, REPL/`llm "질문"` 실행 방식, 단순한 세션 메뉴와 안전한 권한 기본값입니다. 과거 버전별 상세 변경 이력·폐기된 사용법은 이 문서에서 제거했습니다.
