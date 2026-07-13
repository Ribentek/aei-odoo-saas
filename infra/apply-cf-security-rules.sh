#!/usr/bin/env bash
# infra/apply-cf-security-rules.sh
#
# Applies Cloudflare edge security rules for the Odoo SaaS portal via the
# Rulesets API. Remediates pentest findings at the edge (defense-in-depth;
# the in-cluster Traefik/Odoo layers enforce the same controls):
#
#   * Block sensitive paths (VULN-0001 def-in-depth, VULN-0003, VULN-0012):
#       /web/database/*, /xmlrpc/*, /jsonrpc, /website/info  -> 403
#   * Rate-limit auth endpoints (VULN-0007):
#       /web/login, /web/signup, /web/reset_password
#
# SAFE BY DESIGN (this zone has other production rules that must NOT break):
#   * NON-DESTRUCTIVE: appends/updates only its OWN rules (matched by description).
#     Existing rules in the same phase are read and preserved — it never does a
#     blind PUT that replaces the whole ruleset (unless the phase has no ruleset
#     at all, in which case it creates one containing only our rule).
#   * HOST-SCOPED: every rule only matches CF_PROTECTED_HOSTS, so other domains
#     on the zone are never affected even when a rule fires.
#   * DRY-RUN by default: prints what it would do. Set CONFIRM=1 to actually write.
#
# HTTP security response headers (HSTS/X-Frame/CSP/Referrer) are NOT set here —
# they live in-repo in k8s/03-traefik-middleware.yaml.
#
# Requires: CF_API_TOKEN (Zone.WAF + Zone.Ruleset edit), CF_ZONE_ID, jq, curl.
# Scope:    CF_PROTECTED_HOSTS (space-separated). Defaults to staging only.
#           After validating staging, extend and re-run, e.g.:
#             CF_PROTECTED_HOSTS="staging.aeisoftware.com www.aeisoftware.com admin.aeisoftware.com"
#
# Usage:
#   # 1) dry-run first (no writes):
#   CF_API_TOKEN=... CF_ZONE_ID=... ./infra/apply-cf-security-rules.sh
#   # 2) apply for real:
#   CONFIRM=1 CF_API_TOKEN=... CF_ZONE_ID=... ./infra/apply-cf-security-rules.sh
set -euo pipefail

: "${CF_API_TOKEN:?set CF_API_TOKEN}"
: "${CF_ZONE_ID:?set CF_ZONE_ID}"
CF_PROTECTED_HOSTS="${CF_PROTECTED_HOSTS:-staging.aeisoftware.com}"
WRITE="${CONFIRM:-0}"
API="https://api.cloudflare.com/client/v4"

command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }

# CF-expression host set: (http.host in {"a" "b"})
host_set=""
for h in $CF_PROTECTED_HOSTS; do host_set="${host_set}\"${h}\" "; done
HOST_EXPR="(http.host in {${host_set% }})"

echo "==> Zone:            $CF_ZONE_ID"
echo "==> Protected hosts: $CF_PROTECTED_HOSTS"
if [ "$WRITE" = "1" ]; then
  echo "==> MODE:            WRITE (CONFIRM=1)"
else
  echo "==> MODE:            DRY-RUN — no changes. Re-run with CONFIRM=1 to apply."
fi

# Emits the JSON body, then a final line "__HTTP__<status>" so callers can branch on it.
cf() {  # cf METHOD PATH [DATA]
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl -s -w '\n__HTTP__%{http_code}' -X "$method" "${API}${path}" \
      -H "Authorization: Bearer ${CF_API_TOKEN}" -H "Content-Type: application/json" -d "$data"
  else
    curl -s -w '\n__HTTP__%{http_code}' -X "$method" "${API}${path}" \
      -H "Authorization: Bearer ${CF_API_TOKEN}" -H "Content-Type: application/json"
  fi
}

cf_body()   { printf '%s' "$1" | sed 's/__HTTP__[0-9]*$//'; }
cf_status() { printf '%s' "${1##*__HTTP__}"; }

# apply_rule PHASE DESCRIPTION RULE_JSON  — append or update our rule, preserve the rest
apply_rule() {
  local phase="$1" desc="$2" rule="$3"
  echo ""
  echo "--- phase: $phase"
  echo "    rule:  $desc"

  local resp code ep rid total existing_id
  resp=$(cf GET "/zones/${CF_ZONE_ID}/rulesets/phases/${phase}/entrypoint")
  code=$(cf_status "$resp"); ep=$(cf_body "$resp")

  # Only a genuine 404 means "no ruleset yet". 401/403 = permission problem — never
  # treat that as a reason to write; 5xx/other = abort too. This avoids a destructive
  # create/overwrite when the API is merely denying us.
  if [ "$code" = "401" ] || [ "$code" = "403" ]; then
    echo "    ABORT: permission denied (HTTP $code) — token lacks Zone WAF/Ruleset edit. No write attempted."
    echo "$ep" | jq -c '.errors' 2>/dev/null | sed 's/^/    /'
    return 1
  fi
  if [ "$code" != "200" ] && [ "$code" != "404" ]; then
    echo "    ABORT: unexpected HTTP $code. No write attempted."
    echo "$ep" | jq -c '.errors' 2>/dev/null | sed 's/^/    /'
    return 1
  fi

  if [ "$code" = "404" ]; then
    echo "    no existing entrypoint ruleset in this phase -> CREATE with our rule only"
    local payload; payload=$(jq -n --argjson r "$rule" '{rules:[$r]}')
    if [ "$WRITE" = "1" ]; then
      cf_body "$(cf PUT "/zones/${CF_ZONE_ID}/rulesets/phases/${phase}/entrypoint" "$payload")" \
        | jq '{success, errors, rules: (.result.rules // [] | length)}'
    else
      echo "    [dry-run] would PUT:"; echo "$payload" | jq .
    fi
    return
  fi

  rid=$(echo "$ep" | jq -r '.result.id')
  total=$(echo "$ep" | jq '.result.rules | length')
  existing_id=$(echo "$ep" | jq -r --arg d "$desc" '(.result.rules[]? | select(.description==$d) | .id) // empty' | head -1)
  echo "    existing ruleset $rid has $total rule(s) — ALL preserved"

  if [ -n "$existing_id" ]; then
    echo "    our rule already present ($existing_id) -> PATCH (update in place)"
    if [ "$WRITE" = "1" ]; then
      cf_body "$(cf PATCH "/zones/${CF_ZONE_ID}/rulesets/${rid}/rules/${existing_id}" "$rule")" \
        | jq '{success, errors}'
    else
      echo "    [dry-run] would PATCH rule $existing_id"
    fi
  else
    echo "    our rule absent -> POST (append; existing rules untouched)"
    if [ "$WRITE" = "1" ]; then
      cf_body "$(cf POST "/zones/${CF_ZONE_ID}/rulesets/${rid}/rules" "$rule")" \
        | jq '{success, errors}'
    else
      echo "    [dry-run] would POST:"; echo "$rule" | jq .
    fi
  fi
}

# ── Rule 1: block sensitive paths (custom firewall phase) ───────────────────
# Uses starts_with() (not regex `matches`) so it works on the Cloudflare Free plan.
BLOCK_EXPR="${HOST_EXPR} and ("
# starts_with (not regex `matches`) — regex operators require Cloudflare Pro+.
BLOCK_EXPR+='starts_with(http.request.uri.path, "/web/database") or '
BLOCK_EXPR+='starts_with(http.request.uri.path, "/xmlrpc/") or '
BLOCK_EXPR+='http.request.uri.path eq "/jsonrpc" or '
BLOCK_EXPR+='http.request.uri.path eq "/website/info"'
BLOCK_EXPR+=")"
BLOCK_DESC="AEI SaaS: block DB manager / RPC / info (VULN-0001/0003/0012)"
BLOCK_RULE=$(jq -n --arg e "$BLOCK_EXPR" --arg d "$BLOCK_DESC" \
  '{action:"block", expression:$e, description:$d, enabled:true}')
apply_rule "http_request_firewall_custom" "$BLOCK_DESC" "$BLOCK_RULE"

# ── Rule 2: rate-limit auth endpoints (rate-limit phase) ────────────────────
RL_EXPR="${HOST_EXPR} and "
RL_EXPR+='(http.request.uri.path in {"/web/login" "/web/signup" "/web/reset_password"})'
RL_DESC="AEI SaaS: rate-limit auth endpoints (VULN-0007)"
# period/mitigation_timeout of 10s are the only values allowed on the Cloudflare
# Free plan ("not entitled to use the period 60, can only use a period among [10]").
RL_RULE=$(jq -n --arg e "$RL_EXPR" --arg d "$RL_DESC" '{
  action:"block", expression:$e, description:$d, enabled:true,
  ratelimit:{characteristics:["ip.src","cf.colo.id"], period:10, requests_per_period:10, mitigation_timeout:10}
}')
apply_rule "http_ratelimit" "$RL_DESC" "$RL_RULE"

echo ""
echo "==> Done. Verify (first host):"
FIRST="${CF_PROTECTED_HOSTS%% *}"
echo "    curl -sI https://${FIRST}/web/database/manager        # expect 403"
echo "    for i in \$(seq 1 30); do curl -s -o /dev/null -w '%{http_code}\\n' https://${FIRST}/web/login; done  # 429 after ~20"
