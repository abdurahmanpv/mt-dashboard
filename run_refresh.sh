#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_refresh.sh  —  CEO Subscription Dashboard daily refresh
#
# Runs the pipeline, then git-pushes the updated HTML to GitHub Pages.
#
# Usage:
#   ./run_refresh.sh              # full run: MySQL → Excel → HTML → push
#   ./run_refresh.sh --skip-db    # rebuild from existing Excel (no DB call)
#   ./run_refresh.sh --dry-run    # validate only, no changes made
#
# Cron example (runs daily at 11:00 PM server time):
#   0 23 * * * /path/to/repo/run_refresh.sh >> /path/to/repo/logs/cron.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/refresh_$(date +%F).log"
SKIP_DB=false
DRY_RUN=false

# ── Parse args ────────────────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --skip-db)  SKIP_DB=true  ;;
    --dry-run)  DRY_RUN=true  ;;
  esac
done

# ── Ensure logs/ exists ───────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "========================================"
log " CEO Dashboard refresh starting"
log "========================================"

# ── Activate venv ─────────────────────────────────────────────────────────────
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  log "ERROR: venv not found at $VENV_PYTHON"
  log "Run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

# ── Load .env if present (analytics server may use env vars instead) ──────────
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -o allexport
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +o allexport
  log ".env loaded"
fi

# ── Build Python args ─────────────────────────────────────────────────────────
PY_ARGS=("$SCRIPT_DIR/daily_refresh.py")
$SKIP_DB  && PY_ARGS+=("--skip-db")
$DRY_RUN  && PY_ARGS+=("--dry-run")

# ── Run pipeline ──────────────────────────────────────────────────────────────
cd "$SCRIPT_DIR"
"$VENV_PYTHON" "${PY_ARGS[@]}" 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE="${PIPESTATUS[0]}"

if [[ "$EXIT_CODE" -eq 0 ]]; then
  log "Pipeline finished successfully (exit 0)"
else
  log "Pipeline FAILED with exit code $EXIT_CODE"
  exit "$EXIT_CODE"
fi

# ── Push updated HTML to GitHub (skipped on --dry-run) ───────────────────────
if ! $DRY_RUN; then
  HTML_FILE="$SCRIPT_DIR/CEO_Subscription_Dashboard.html"

  if [[ ! -f "$HTML_FILE" ]]; then
    log "WARNING: CEO_Subscription_Dashboard.html not found — skipping push"
    exit 0
  fi

  git -C "$SCRIPT_DIR" add CEO_Subscription_Dashboard.html

  if git -C "$SCRIPT_DIR" diff --cached --quiet; then
    log "HTML unchanged — skipping commit"
  else
    DATE_STR="$(date -u +%Y-%m-%d)"
    git -C "$SCRIPT_DIR" \
      -c user.name="cron-refresh" \
      -c user.email="cron@noreply" \
      commit -m "chore: refresh dashboard $DATE_STR" 2>&1 | tee -a "$LOG_FILE"

    if git -C "$SCRIPT_DIR" push origin main 2>&1 | tee -a "$LOG_FILE"; then
      log "HTML pushed → GitHub Pages will update shortly"
      log "URL: https://abdurahmanpv.github.io/mt-dashboard/CEO_Subscription_Dashboard.html"
    else
      log "WARNING: git push failed — HTML not published"
      exit 1
    fi
  fi
fi

# ── Prune logs older than 30 days ─────────────────────────────────────────────
find "$LOG_DIR" -name "refresh_*.log" -mtime +30 -delete 2>/dev/null || true

log "Done."
