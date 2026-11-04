#!/usr/bin/env bash
# Run a one-off ECS Fargate task (db-bootstrap, migrate) from a deploy manifest and fail if the
# container exits non-zero. Used by .github/workflows/deploy.yml; runnable by hand with admin creds.
#
#   infra/scripts/run_ecs_task.sh <manifest-ssm-parameter> <task-family> [image]
#
# If [image] is given, a new task-definition revision with that image is registered first.
set -euo pipefail

MANIFEST_PARAM="$1"
FAMILY="$2"
IMAGE="${3:-}"

manifest="$(aws ssm get-parameter --name "$MANIFEST_PARAM" --query Parameter.Value --output text)"
cluster="$(jq -r .cluster <<<"$manifest")"
subnets="$(jq -r '.subnets | join(",")' <<<"$manifest")"
sg="$(jq -r .security_group <<<"$manifest")"

HERE="$(cd "$(dirname "$0")" && pwd)"
taskdef="$FAMILY"
if [[ -n "$IMAGE" ]]; then
  taskdef="$("$HERE/register_task_def.sh" "$FAMILY" "$IMAGE")"
fi

echo "::group::run ${taskdef} on ${cluster}"
task_arn="$(aws ecs run-task \
  --cluster "$cluster" \
  --task-definition "$taskdef" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${subnets}],securityGroups=[${sg}],assignPublicIp=DISABLED}" \
  --started-by "github-${GITHUB_RUN_ID:-manual}" \
  --query 'tasks[0].taskArn' --output text)"
echo "task: ${task_arn}"

aws ecs wait tasks-stopped --cluster "$cluster" --tasks "$task_arn"

result="$(aws ecs describe-tasks --cluster "$cluster" --tasks "$task_arn" \
  --query 'tasks[0].{exit:containers[0].exitCode,reason:stoppedReason,container:containers[0].reason}' --output json)"
echo "$result"
echo "::endgroup::"

exit_code="$(jq -r '.exit // "null"' <<<"$result")"
if [[ "$exit_code" != "0" ]]; then
  echo "::error::${FAMILY} failed (exit ${exit_code}); see CloudWatch log group /kx/${FAMILY}"
  exit 1
fi
