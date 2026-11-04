#!/usr/bin/env bash
# Deploy one company in one environment:
#   1. record the image tag in SSM (Terraform reads it back, so applies never roll back a deploy)
#   2. db-bootstrap (idempotent)  3. kx migrate  4. roll api/worker/voice (+ cms) and wait
# ECS deployment circuit breaker (rollback = true) reverts a failed rollout; this script then fails.
#
#   infra/scripts/deploy_company.sh <env> <company> <app-tag> [cms-tag]
#   e.g. infra/scripts/deploy_company.sh prod pack v1.4.0
set -euo pipefail

ENV_NAME="$1"
COMPANY="$2"
APP_TAG="$3"
CMS_TAG="${4:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"

param="/kx/${ENV_NAME}/${COMPANY}/deploy"
manifest="$(aws ssm get-parameter --name "$param" --query Parameter.Value --output text)"
cluster="$(jq -r .cluster <<<"$manifest")"
app_image="$(jq -r .app_repository <<<"$manifest"):${APP_TAG}"
cms_image="$(jq -r .cms_repository <<<"$manifest"):${CMS_TAG}"
bootstrap="$(jq -r .db_bootstrap <<<"$manifest")"
migrate="$(jq -r .migrate_family <<<"$manifest")"
mapfile -t services < <(jq -r '.services[]' <<<"$manifest")
cms_service="$(jq -r '.cms_service // empty' <<<"$manifest")"

echo "deploy ${COMPANY}/${ENV_NAME}: ${app_image}"
aws ssm put-parameter --name "$(jq -r .app_tag_param <<<"$manifest")" --value "$APP_TAG" --overwrite >/dev/null

"$HERE/run_ecs_task.sh" "$param" "$bootstrap" "$app_image"
"$HERE/run_ecs_task.sh" "$param" "$migrate" "$app_image"

rolled=()
for svc in "${services[@]}"; do
  td="$("$HERE/register_task_def.sh" "$svc" "$app_image")"
  aws ecs update-service --cluster "$cluster" --service "$svc" --task-definition "$td" >/dev/null
  rolled+=("$svc")
done

if [[ -n "$CMS_TAG" && -n "$cms_service" ]]; then
  aws ssm put-parameter --name "$(jq -r .cms_tag_param <<<"$manifest")" --value "$CMS_TAG" --overwrite >/dev/null
  td="$("$HERE/register_task_def.sh" "$cms_service" "$cms_image")"
  aws ecs update-service --cluster "$cluster" --service "$cms_service" --task-definition "$td" >/dev/null
  rolled+=("$cms_service")
fi

echo "waiting for ${rolled[*]} to become stable"
aws ecs wait services-stable --cluster "$cluster" --services "${rolled[@]}"

# shellcheck disable=SC2016  # JMESPath backticks, not shell
failed="$(aws ecs describe-services --cluster "$cluster" --services "${rolled[@]}" \
  --query 'services[].{s:serviceName,state:deployments[?status==`PRIMARY`]|[0].rolloutState}' --output json \
  | jq -r '.[] | select(.state != "COMPLETED") | "\(.s)=\(.state)"')"
if [[ -n "$failed" ]]; then
  echo "::error::rollout not completed (circuit breaker may have rolled back): ${failed}"
  exit 1
fi
echo "deploy ${COMPANY}/${ENV_NAME}: done"
