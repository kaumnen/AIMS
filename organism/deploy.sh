#!/usr/bin/env bash
# Fireworks deployment of a promoted Glimmer adapter: H200 BF16 live merge, $8/h while up.
#   organism/deploy.sh create <model-id> <deployment-id>
#   organism/deploy.sh show <deployment-id>
#   organism/deploy.sh list
#   organism/deploy.sh delete <deployment-id>     # always delete after use, then list
#   organism/deploy.sh spend                      # month-to-date spend from the quota
# Needs FIREWORKS_API_KEY and FIREWORKS_ACCOUNT_ID, from the environment or ./.env.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
A=${FIREWORKS_ACCOUNT_ID:?set FIREWORKS_ACCOUNT_ID}
B=https://api.fireworks.ai/v1/accounts/$A
H=(-H "Authorization: Bearer $FIREWORKS_API_KEY")
case "$1" in
create)
  curl -s -X POST "$B/deployments?deploymentId=$3" "${H[@]}" -H 'Content-Type: application/json' -d "{
 \"baseModel\":\"accounts/$A/models/$2\",
 \"displayName\":\"AIMS $3\",
 \"deploymentShape\":\"accounts/fireworks/deploymentShapes/dkuwotf7/versions/vtcy28lv\",
 \"minReplicaCount\":0, \"maxReplicaCount\":1,
 \"autoscalingPolicy\":{\"scaleToZeroWindow\":\"600s\"}}" ;;
show) curl -s "$B/deployments/$2" "${H[@]}" ;;
list) curl -s "$B/deployments" "${H[@]}" ;;
delete) curl -s -X DELETE "$B/deployments/$2?ignoreChecks=true" "${H[@]}" ;;
spend) curl -s "$B/quotas" "${H[@]}" | python3 -c "import json,sys; [print(q['name'].split('/')[-1], q.get('usage'), 'of', q.get('value')) for q in json.load(sys.stdin)['quotas'] if 'spend' in q['name']]" ;;
*) echo "unknown command $1" >&2; exit 2 ;;
esac
echo
