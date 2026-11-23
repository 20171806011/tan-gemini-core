#!/usr/bin/env bash
# Portable 1-command 30-minute cron installer for any Linux VPS / container host.
# Ensures TAN Gemini Core can migrate off GitHub Actions with zero vendor lock-in.
set -euo pipefail

REPO_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON_BIN="$(command -v python3)"
LOG_FILE="${HOME}/.tan-gemini-core.log"
CRON_LINE="11,41 * * * * cd \"${REPO_DIR}\" && git pull --rebase --autostash origin main >/dev/null 2>&1 || true; \"${PYTHON_BIN}\" runtime/survival_cycle.py --runner vps_cron >> \"${LOG_FILE}\" 2>&1"

if ! command -v crontab >/dev/null 2>&1; then
  echo "crontab not installed on this host; run scripts/local_supervisor.py instead." >&2
  exit 1
fi

(crontab -l 2>/dev/null | grep -v "runtime/survival_cycle.py" || true; echo "${CRON_LINE}") | crontab -
echo "Installed 30-minute TAN Gemini Core cron entry (UTC :11 and :41):"
crontab -l | grep "runtime/survival_cycle.py"
