#!/usr/bin/env bash
#
# Installer for jkanime-scraper.
# Creates a virtualenv, installs Python deps, and checks downloader tools.
#
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "==> Creating virtualenv (.venv)"
python3 -m venv .venv

echo "==> Installing Python dependencies"
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "==> Checking download tools"
missing=0
for tool in wget yt-dlp; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "    OK  $tool ($("$tool" --version 2>/dev/null | head -1))"
  else
    echo "    !!  $tool not found"
    missing=1
  fi
done

if [ "$missing" -ne 0 ]; then
  echo ""
  echo "Install the missing tools, e.g.:"
  echo "  sudo apt install wget"
  echo "  sudo apt install yt-dlp   # or: pip install yt-dlp"
fi

echo ""
echo "Done. Usage:"
echo "  source .venv/bin/activate"
echo "  python jkanime_scraper.py --url https://jkanime.net/<series>/1/"
echo "  bash <series>/download.sh"
