#!/usr/bin/env bash
# Bulk-upload local DNS records to a Pi-hole v6 instance via the REST API.
#
# Input file: one record per line, blank lines and `#` comments ignored.
#   hostname  ip
#   router.lan  192.168.1.1
#   nas         192.168.1.20
#
# Usage:
#   ./upload_dns_records.sh -u https://pi.hole -p SECRET records.txt
#   ./upload_dns_records.sh -u http://pi.hole --no-password records.txt
#   ./upload_dns_records.sh -u https://pi.hole -p SECRET -k records.txt   # skip TLS verify

set -euo pipefail

URL=""
PASSWORD=""
NO_PASSWORD=0
INSECURE=0
FILE=""

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-1}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -u|--url)         URL="$2"; shift 2 ;;
        -p|--password)    PASSWORD="$2"; shift 2 ;;
        --no-password)    NO_PASSWORD=1; shift ;;
        -k|--insecure)    INSECURE=1; shift ;;
        -h|--help)        usage 0 ;;
        -*)               echo "Unknown option: $1" >&2; usage ;;
        *)                FILE="$1"; shift ;;
    esac
done

[[ -n "$URL"  ]] || { echo "Missing --url" >&2; usage; }
[[ -n "$FILE" ]] || { echo "Missing input file" >&2; usage; }
[[ -f "$FILE" ]] || { echo "File not found: $FILE" >&2; exit 1; }
if [[ -z "$PASSWORD" && "$NO_PASSWORD" -eq 0 ]]; then
    echo "Provide --password or --no-password" >&2; usage
fi

URL="${URL%/}"
CURL=(curl -sS)
[[ "$INSECURE" -eq 1 ]] && CURL+=(-k)

# URL-encode using jq (already a dependency for parsing the auth response).
urlencode() { jq -rn --arg v "$1" '$v|@uri'; }

SID=""
if [[ "$NO_PASSWORD" -eq 0 ]]; then
    AUTH_RESP=$("${CURL[@]}" -X POST -H 'Content-Type: application/json' \
        -d "$(jq -n --arg p "$PASSWORD" '{password:$p}')" \
        "$URL/api/auth")
    SID=$(echo "$AUTH_RESP" | jq -r '.session.sid // empty')
    if [[ -z "$SID" ]]; then
        echo "Authentication failed: $AUTH_RESP" >&2
        exit 1
    fi
fi

logout() {
    [[ -n "$SID" ]] && "${CURL[@]}" -X DELETE -H "X-FTL-SID: $SID" "$URL/api/auth" >/dev/null || true
}
trap logout EXIT

OK=0
FAIL=0
LINENO_=0
while IFS= read -r RAW || [[ -n "$RAW" ]]; do
    LINENO_=$((LINENO_ + 1))
    LINE="${RAW%%#*}"
    LINE="$(echo "$LINE" | xargs)"
    [[ -z "$LINE" ]] && continue

    read -r HOST IP EXTRA <<< "$LINE"
    if [[ -z "$HOST" || -z "$IP" || -n "$EXTRA" ]]; then
        echo "  ! line $LINENO_: expected 'hostname ip', got: $RAW" >&2
        FAIL=$((FAIL + 1))
        continue
    fi

    ELEMENT=$(urlencode "$IP $HOST")
    HEADERS=()
    [[ -n "$SID" ]] && HEADERS=(-H "X-FTL-SID: $SID")

    RESP=$("${CURL[@]}" -o /dev/null -w '%{http_code}' -X PUT \
        "${HEADERS[@]}" "$URL/api/config/dns/hosts/$ELEMENT") || RESP="000"

    if [[ "$RESP" =~ ^2 ]]; then
        printf '  + %-15s  %s\n' "$IP" "$HOST"
        OK=$((OK + 1))
    else
        printf '  ! %-15s  %s  — HTTP %s\n' "$IP" "$HOST" "$RESP" >&2
        FAIL=$((FAIL + 1))
    fi
done < "$FILE"

echo
echo "Done. $OK added, $FAIL failed."
[[ "$FAIL" -eq 0 ]]
