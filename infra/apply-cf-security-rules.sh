#!/usr/bin/env bash
# infra/apply-cf-security-rules.sh
#
# Applies Cloudflare edge security rules for the Odoo SaaS portal via the
# Rulesets API. Remediates pentest findings at the edge (defense-in-depth;
# the in-cluster Traefik/Odoo layers enforce the same controls):
#
#   * Block sensitive paths (VULN-0001 def-in-depth, VULN-0003, VULN-0012):
#       /web/database/*, /xmlrpc/*, /jsonrpc, /website/info  → 403
#   * Rate-limit auth endpoints (VULN-0007):
#       /web/login, /web/signup, /web/reset_password
#
# HTTP security response headers (HSTS/X-Frame-Options/CSP/Referrer-Policy) are
# NOT set here — they live in-repo in k8s/03-traefik-middleware.yaml.
#
# Idempotent: each PUT replaces the phase entrypoint ruleset for the zone.
#
# Requires: CF_API_TOKEN (Zone.WAF + Zone.Ruleset edit), CF_ZONE_ID, jq, curl.
# Scope:    CF_PROTECTED_HOSTS — space-separated hostnames the rules apply to.
#           Defaults to staging only (staging-first rollout). Add www/admin
#           after staging validation, e.g.:
#             CF_PROTECTED_HOSTS="staging.aeisoftware.com www.aeisoftware.com admin.aeisoftware.com"
#
# Usage:
#   CF_API_TOKEN=... CF_ZONE_ID=... ./infra/apply-cf-security-rules.sh
#   DRY_RUN=1 ... ./infra/apply-cf-security-rules.sh   # print payloads, no writes
set -euo pipefail

: "${CF_API_TOKEN:?set CF_API_TOKEN}"
: "${CF_ZONE_ID:?set CF_ZONE_ID}"
CF_PROTECTED_HOSTS="${CF_PROTECTED_HOSTS:-staging.aeisoftware.com}"
API="https://api.cloudflare.com/client/v4"

# Build a CF-expression host set: (http.host in {"a" "b"})
host_set=""
for h in $CF_PROTECTED_HOSTS; do
  host_set="${host_set}\"${h}\" "
done
HOST_EXPR="(http.host in {${host_set% }})"

echo "==> Protected hosts: ${CF_PROTECTED_HOSTS}"

cf_put() {
  local phase="$1" payload="$2"
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "--- DRY_RUN ${phase} ---"
    echo "${payload}" | jq .
    return 0
  fi
  curl -s -X PUT \
    "${API}/zones/${CF_ZONE_ID}/rulesets/phases/${phase}/entrypoint" \
    -H "Authorization: Bearer ${CF_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "${payload}" | jq '{success, errors, messages, rules: (.result.rules // [] | length)}'
}

# ── 1. Block sensitive paths (custom firewall phase) ────────────────────────
BLOCK_EXPR="${HOST_EXPR} and ("
BLOCK_EXPR+='http.request.uri.path matches "^/web/database/" or '
BLOCK_EXPR+='http.request.uri.path eq "/web/database/manager" or '
BLOCK_EXPR+='http.request.uri.path matches "^/xmlrpc/" or '
BLOCK_EXPR+='http.request.uri.path eq "/jsonrpc" or '
BLOCK_EXPR+='http.request.uri.path eq "/website/info"'
BLOCK_EXPR+=")"

BLOCK_PAYLOAD=$(jq -n --arg expr "${BLOCK_EXPR}" '{
  rules: [{
    action: "block",
    expression: $expr,
    description: "AEI SaaS: block DB manager / RPC / info at edge (VULN-0001/0003/0012)",
    enabled: true
  }]
}')

echo "==> Applying path-block rules (http_request_firewall_custom) …"
cf_put "http_request_firewall_custom" "${BLOCK_PAYLOAD}"

# ── 2. Rate-limit auth endpoints (rate-limit phase) ─────────────────────────
RL_EXPR="${HOST_EXPR} and "
RL_EXPR+='(http.request.uri.path in {"/web/login" "/web/signup" "/web/reset_password"})'

RL_PAYLOAD=$(jq -n --arg expr "${RL_EXPR}" '{
  rules: [{
    action: "block",
    expression: $expr,
    description: "AEI SaaS: rate-limit auth endpoints (VULN-0007)",
    enabled: true,
    ratelimit: {
      characteristics: ["ip.src", "cf.colo.id"],
      period: 60,
      requests_per_period: 20,
      mitigation_timeout: 600
    }
  }]
}')

echo "==> Applying auth rate-limit rules (http_ratelimit) …"
cf_put "http_ratelimit" "${RL_PAYLOAD}"

echo "==> Done. Verify:"
echo "    curl -sI https://${CF_PROTECTED_HOSTS%% *}/web/database/manager   # expect 403"
echo "    for i in \$(seq 1 30); do curl -s -o /dev/null -w '%{http_code}\\n' \\"
echo "        https://${CF_PROTECTED_HOSTS%% *}/web/login; done            # expect 429 after ~20"
