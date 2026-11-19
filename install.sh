#!/usr/bin/env bash
# Run once after cloning: bash install.sh
# Installs a user-local 'llm' command without sudo.
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LAUNCHER="$REPO_DIR/llm"
BIN_DIR="${HOME}/.local/bin"
TARGET="$BIN_DIR/llm"
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'

if [[ ! -f "$REPO_DIR/agent.py" || ! -f "$LAUNCHER" ]]; then
  echo "오류: 저장소 루트에서 install.sh를 실행하세요." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "오류: Python 3.10 이상이 필요합니다 (python3 명령 없음)." >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "오류: Python 3.10 이상이 필요합니다." >&2
  exit 1
fi

mkdir -p "$BIN_DIR"
chmod u+x "$LAUNCHER"
if [[ -e "$TARGET" && ! -L "$TARGET" ]]; then
  echo "오류: $TARGET 파일이 이미 있습니다. 안전을 위해 덮어쓰지 않습니다." >&2
  exit 1
fi
ln -sfn "$LAUNCHER" "$TARGET"

# Login shell (.profile) and interactive bash (.bashrc) support.
for profile in "$HOME/.profile" "$HOME/.bashrc"; do
  if [[ -e "$profile" && ! -f "$profile" ]]; then
    echo "경고: 일반 파일이 아닌 $profile 은 수정하지 않았습니다."
    continue
  fi
  if [[ ! -f "$profile" ]] || ! grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$profile"; then
    printf '\n# llm command installed by install.sh\n%s\n' "$PATH_LINE" >> "$profile"
  fi
done

export PATH="$BIN_DIR:$PATH"
echo "설치 완료: $TARGET -> $LAUNCHER"
echo "다른 디렉터리에서 실행: llm help"
echo "현재 셸에 바로 적용하려면: export PATH=\"\$HOME/.local/bin:\$PATH\""
echo "새 터미널에서는 별도 설정 없이 사용 가능합니다."
