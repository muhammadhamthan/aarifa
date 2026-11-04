#!/usr/bin/env bash
# Register a new revision of an ECS task-definition family with a different image for its
# (single) container, keeping everything else Terraform defined. Prints the new revision ARN.
#
#   infra/scripts/register_task_def.sh <family> <image>
set -euo pipefail

FAMILY="$1"
IMAGE="$2"

current="$(aws ecs describe-task-definition --task-definition "$FAMILY" --include TAGS --output json)"

jq --arg image "$IMAGE" '
  .taskDefinition
  | .containerDefinitions |= map(.image = $image)
  | {family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions, volumes,
     placementConstraints, requiresCompatibilities, cpu, memory, runtimePlatform}
  | with_entries(select(.value != null))
' <<<"$current" >"${RUNNER_TEMP:-/tmp}/taskdef-${FAMILY}.json"

tags="$(jq -c '.tags // []' <<<"$current")"
args=(--cli-input-json "file://${RUNNER_TEMP:-/tmp}/taskdef-${FAMILY}.json")
if [[ "$tags" != "[]" ]]; then
  args+=(--tags "$tags")
fi

aws ecs register-task-definition "${args[@]}" --query 'taskDefinition.taskDefinitionArn' --output text
