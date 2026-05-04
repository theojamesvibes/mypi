#!/usr/bin/env bash
# End-to-end smoke for a running MyPi instance.
#
# Hits the live HTTP surface against an already-deployed container —
# verifies the basics (/api/health, login, authenticated /me, instance
# list, logout) without touching any state. Use it to gate
# rollouts: deploy, then `./tests/e2e/smoke.sh https://mypi.example`
# to confirm the new image isn't returning 500s on the golden path.
#
# Usage:
#   ./tests/e2e/smoke.sh                        # http://localhost:8080
#   ./tests/e2e/smoke.sh http://192.168.1.5:8080
#   MYPI_USER=admin MYPI_PASSWORD=… ./tests/e2e/smoke.sh https://mypi.example
#
# Auth is optional — if MYPI_USER/MYPI_PASSWORD are set the script
# logs in and exercises the authenticated paths. Without them only the
# unauthenticated /api/health is checked.
#
# Exit code 0 = all checks passed; non-zero = something broke.

set -euo pipefail

HOST="${1:-http://localhost:8080}"
USER="${MYPI_USER:-}"
PASSWORD="${MYPI_PASSWORD:-}"

# Single curl invocation pattern: -sS = quiet but show errors,
# -o body = body to file, -w '%{http_code}' = print status code.
COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT

# ── Helpers ──────────────────────────────────────────────────────────────────

_GREEN=$'\e[32m'
_RED=$'\e[31m'
_DIM=$'\e[2m'
_RESET=$'\e[0m'

passed=0
failed=0

_pass() {
  passed=$((passed + 1))
  printf '  %sPASS%s %s\n' "$_GREEN" "$_RESET" "$1"
}

_fail() {
  failed=$((failed + 1))
  printf '  %sFAIL%s %s\n' "$_RED" "$_RESET" "$1"
  if [[ -n "${2-}" ]]; then
    printf '       %s%s%s\n' "$_DIM" "$2" "$_RESET"
  fi
}

# Pretty-print a section header.
_section() {
  printf '\n%s%s%s\n' "$_DIM" "── $1 ──" "$_RESET"
}

# Run a curl, capture status + body. Args: METHOD URL [extra curl args...]
# Body lands in $body, status in $status.
_request() {
  local method="$1"; shift
  local url="$1"; shift
  local body_file
  body_file="$(mktemp)"
  status=$(curl -sS -X "$method" -o "$body_file" -w '%{http_code}' \
                -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
                "$@" "$url" || echo "curl-error")
  body="$(cat "$body_file")"
  rm -f "$body_file"
}

# Assert a response field exists and is non-empty. Body in $body.
_has_field() {
  local key="$1"
  # Naive — checks for "key": before any value. Works for the simple
  # JSON shapes /api/health and friends return; we don't need a real
  # JSON parser here.
  grep -q "\"$key\":" <<<"$body"
}

# ── /api/health ──────────────────────────────────────────────────────────────

_section "Unauthenticated"

_request GET "$HOST/api/health"
if [[ "$status" == "200" ]] && _has_field "version" && _has_field "stats_poll_interval"; then
  _pass "GET /api/health (200, version + intervals present)"
else
  _fail "GET /api/health" "status=$status body=${body:0:200}"
fi

# ── Authenticated paths ──────────────────────────────────────────────────────

if [[ -z "$USER" || -z "$PASSWORD" ]]; then
  _section "Skipping authenticated checks"
  echo "  ${_DIM}Set MYPI_USER and MYPI_PASSWORD to run the full suite.${_RESET}"
else
  _section "Authenticated"

  _request POST "$HOST/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$USER\",\"password\":\"$PASSWORD\"}"
  if [[ "$status" == "200" ]] && _has_field "access_token"; then
    _pass "POST /api/auth/login (200, access_token returned)"
  else
    _fail "POST /api/auth/login" "status=$status body=${body:0:200}"
    # Without a session cookie, no point running the rest.
    printf '\n%sLogin failed — skipping the remaining authenticated checks.%s\n' \
      "$_DIM" "$_RESET"
    printf '\n%d passed, %d failed\n' "$passed" "$failed"
    exit 1
  fi

  _request GET "$HOST/api/auth/me"
  if [[ "$status" == "200" ]] && _has_field "username"; then
    _pass "GET /api/auth/me (200, username present)"
  else
    _fail "GET /api/auth/me" "status=$status body=${body:0:200}"
  fi

  _request GET "$HOST/api/instances"
  if [[ "$status" == "200" ]]; then
    _pass "GET /api/instances (200)"
  else
    _fail "GET /api/instances" "status=$status body=${body:0:200}"
  fi

  _request GET "$HOST/api/sites"
  if [[ "$status" == "200" ]]; then
    _pass "GET /api/sites (200)"
  else
    _fail "GET /api/sites" "status=$status body=${body:0:200}"
  fi

  _request POST "$HOST/api/auth/logout"
  if [[ "$status" == "200" ]]; then
    _pass "POST /api/auth/logout (200)"
  else
    _fail "POST /api/auth/logout" "status=$status body=${body:0:200}"
  fi

  # After logout the same cookie should no longer authenticate.
  _request GET "$HOST/api/auth/me"
  if [[ "$status" == "401" ]]; then
    _pass "GET /api/auth/me after logout (401, cookie revoked)"
  else
    _fail "Logout didn't actually revoke the session" "status=$status"
  fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────

printf '\n'
if [[ "$failed" -gt 0 ]]; then
  printf '%s%d passed, %d failed%s\n' "$_RED" "$passed" "$failed" "$_RESET"
  exit 1
fi
printf '%s%d passed, %d failed%s\n' "$_GREEN" "$passed" "$failed" "$_RESET"
