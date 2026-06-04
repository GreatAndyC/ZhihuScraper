#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -x "venv/bin/pyinstaller" ]; then
  echo "未找到 venv/bin/pyinstaller，请先执行 make setup 或重新安装 requirements.txt"
  exit 1
fi

export PLAYWRIGHT_BROWSERS_PATH=0
export PYINSTALLER_CONFIG_DIR="$ROOT_DIR/.pyinstaller"
venv/bin/pyinstaller --clean ZhihuScraper.spec

echo "打包完成。输出目录：$ROOT_DIR/dist"
echo "说明：该轻量版应用不会打包 Chromium，请确保系统已安装 Chrome 或 Edge。"
