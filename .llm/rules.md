# llm 프로젝트 규칙

이 문서는 프로그램 개발 규칙이며 Git에서 관리합니다. **대화 기록·API 키·개인정보는 기록하지 않습니다.**

## 구조와 개발 원칙

- `llm`: Ubuntu/Linux Bash 진입점. `install.sh`: Linux 전역 명령 등록. `install.ps1`: Windows 전역 명령 등록(`llm.cmd`).
- `agent.py`: REPL, 에이전트 루프, 로컬 세션·프롬프트, 도구 연결, 터미널 UI.
- `llmlib/`: CLI 인자 해석, 셸 안전 분류, 도구 결과 정리, Markdown, 파일 추적, 로그와 백그라운드 명령.
- `tests/`: 회귀 테스트. `.github/workflows/tests.yml`: CI.
- Python 3.10 이상, 표준 라이브러리 우선. 변경은 작게 나누고 관련 테스트·README를 같이 고칩니다.

## 사용자 명령어

| 외부 | 기능 |
|---|---|
| `llm` | TTY REPL |
| `llm "질문"` | 1회 실행 |
| `llm bal` | 잔액 |
| `llm help` | 도움말 |
| `llm session` | 세션 관리 메뉴 |

- REPL: `/agent`, `/chat`, `/plan`, `/undo`, `/bal`, `/prompt`, `/help`, `/exit`.
- `/plan`은 읽기 전용 도구만 사용하고 파일 수정·위험한 변경을 수행하지 않습니다.
- 이전 명령 별칭은 일부 호환성 용도로 남기되 기본 도움말에는 노출하지 않습니다.
- 사용자에게 보여주는 화면은 필수 상태(프로젝트·세션·모드·로컬 기록) 위주로 유지합니다.

## 대화·프롬프트의 로컬 보관

- `llm/.env`의 `LLM_HISTORY_DIR`가 있으면 지정 경로, 값이 비면 `<현재 작업 폴더>/.llm/`에 세션을 저장합니다. 상대경로는 작업 폴더 기준입니다.
- 세션은 작업 폴더별 바인딩과 소유권을 유지합니다. 다른 프로젝트가 소유한 세션을 자동으로 이어받지 않습니다.
- 전역 프롬프트: `~/.local/share/llm/system.md`; 프로젝트 프롬프트: `<작업 폴더>/.llm/rules.md`; 세션 프롬프트: `<기록 폴더>/prompts/<세션>.md`.
- 사용자 프롬프트는 다음 요청부터 적용하지만 내부 기본 보호 정책을 비활성화하지 않습니다.
- `.env`, `.llm/history_*.json`, `.llm/summaries/`, `.llm/prompts/`와 개인 로그는 Git 제외. `.llm/rules.md`는 공개 문서이므로 비밀을 넣지 않습니다.
- `llm sync`/자동 Push·Pull은 비활성화합니다. 다른 PC로 세션을 동기화하지 않습니다.

## 실행 안전과 비용

- REPL 기본 권한은 safe: 안전한 조회·검사와 작업 폴더 내 일부 명령은 자동 처리하며 위험한 작업은 확인합니다.
- 관리자 권한 상승·설치·비밀 접근·작업 폴더 밖 위험 작업의 보호를 우회하지 않습니다. 프로젝트 밖 삭제·원격 전송·Git 강제 변경은 명시적 검토 대상입니다.
- 모델 도구 실행 결과에서 비밀 문자열을 제거합니다. 프롬프트·로그에 인증키를 노출하지 않습니다.
- 오래된 도구 출력을 먼저 정리하고 필요할 때만 요약 API를 호출합니다. 최근 도구 호출/결과 묶음은 보존하며 요약 직후 재요약을 억제합니다.
- 요약 비용과 일반 응답 비용은 API 사용량으로 확인하며 실제 절감률을 검증하기 전에는 수치를 단정하지 않습니다.

## 설치·검증

- 최초 Clone 후 Ubuntu는 `bash install.sh`, Windows는 `powershell -ExecutionPolicy Bypass -File .\\install.ps1`을 실행합니다. 사용자 PATH만 수정하고 sudo/관리자 권한이 필요하지 않도록 합니다.
- Windows에서 에이전트의 Bash 셸 명령을 사용하려면 Git Bash가 PATH에 있어야 합니다. 새로운 설치 의존성·운영체제별 처리는 문서화합니다.
- `python -m unittest discover -s tests -v`와 문법 검사 통과를 확인합니다. GitHub Actions에서 Ubuntu 및 Windows 설치 명령을 별도 검증합니다.
