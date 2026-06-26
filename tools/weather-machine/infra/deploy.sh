#!/usr/bin/env bash
# Deploy / update the Philadelphia Weather Machine (fixit) stack.
# AWS CLI only (no SAM, no Docker) - the Lambda has no third-party deps.
# Run from this tool's directory:  cd tools/weather-machine && bash infra/deploy.sh
#
# Two modes:
#   Backend-only (DEFAULT) - A2-static hosting. Deploys only the Pattern B
#     backend (SiteBucket, CacheTable, RewriteFunction + role/permission,
#     SlackSecret). The front-end serves as static files from the shared landing
#     host, so no EC2 host, security group, or network inputs are required.
#       bash infra/deploy.sh
#   A1 dedicated stack - also stands up the EC2 front-end host. Requires the
#     builder-provisioned network inputs and the ALB/Okta gate (see DEPLOYMENT.md).
#       DEPLOY_WEB_HOST=true VPC_ID=... SUBNET_ID=... ALB_SG_ID=... bash infra/deploy.sh
set -euo pipefail

# Only pass --profile when AWS_PROFILE is explicitly set; otherwise let the AWS
# CLI use ambient credentials (CloudShell, instance roles, etc.), which have no
# named "default" profile.
PROFILE_ARGS=()
if [[ -n "${AWS_PROFILE:-}" ]]; then PROFILE_ARGS=(--profile "$AWS_PROFILE"); fi
REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK_NAME:-weather-machine}"
CONTACT="${CONTACT_EMAIL:-weather-machine@inquirer.com}"
SITE_URL="${SITE_URL:-}"
DEPLOY_WEB_HOST="${DEPLOY_WEB_HOST:-false}"

ACCOUNT="$(aws sts get-caller-identity "${PROFILE_ARGS[@]}" --query Account --output text)"
DEPLOY_BUCKET="${DEPLOY_BUCKET:-weather-machine-deploy-${ACCOUNT}}"
ASSETS_KEY="web-assets.tar.gz"

if [ "$DEPLOY_WEB_HOST" = "true" ]; then MODE="A1-dedicated"; else MODE="backend-only"; fi
echo "Account: $ACCOUNT | Profile: ${AWS_PROFILE:-<ambient>} | Region: $REGION | Stack: $STACK | Mode: $MODE"

# 1. One-time deploy bucket for the Lambda zip (+ the web bundle in A1 mode).
if ! aws s3api head-bucket --bucket "$DEPLOY_BUCKET" "${PROFILE_ARGS[@]}" 2>/dev/null; then
  echo "Creating deploy bucket $DEPLOY_BUCKET ..."
  aws s3api create-bucket --bucket "$DEPLOY_BUCKET" "${PROFILE_ARGS[@]}" --region "$REGION"
fi

# 2. Lambda source dir (only the two files the function needs). Both modes.
rm -rf build && mkdir build
cp pipeline/lambda_function.py pipeline/rewrite_common.py build/

# 3. Per-mode CloudFormation parameter overrides.
PARAMS=( "ContactEmail=${CONTACT}" "SiteUrl=${SITE_URL}" "DeployWebHost=${DEPLOY_WEB_HOST}" )

if [ "$DEPLOY_WEB_HOST" = "true" ]; then
  # A1 dedicated stack: network inputs required; upload the front-end bundle for
  # the EC2 UserData to pull at boot.
  : "${VPC_ID:?set VPC_ID (the VPC for the front-end host)}"
  : "${SUBNET_ID:?set SUBNET_ID (a subnet the ALB can reach)}"
  : "${ALB_SG_ID:?set ALB_SG_ID (the ALB security group id)}"

  rm -rf .assets && mkdir -p .assets/site
  cp -r site/. .assets/site/
  cp infra/feed-sync.sh .assets/
  cp -r infra/systemd .assets/systemd
  tar -czf web-assets.tar.gz -C .assets .
  aws s3 cp web-assets.tar.gz "s3://${DEPLOY_BUCKET}/${ASSETS_KEY}" \
    "${PROFILE_ARGS[@]}" --region "$REGION"

  PARAMS+=( "VpcId=${VPC_ID}" "SubnetId=${SUBNET_ID}" "AlbSecurityGroupId=${ALB_SG_ID}" \
            "AssetsBucket=${DEPLOY_BUCKET}" "AssetsKey=${ASSETS_KEY}" )
fi
# Backend-only mode: no network guards, no web bundle. VpcId/SubnetId/
# AlbSecurityGroupId/AssetsBucket fall back to the template's "" defaults and the
# DeployWebHost=false condition leaves every front-end resource uncreated.

# 4. Package (zip + upload the Lambda) and deploy the stack.
aws cloudformation package \
  --template-file infra/template.yaml \
  --s3-bucket "$DEPLOY_BUCKET" \
  --output-template-file infra/packaged.yaml \
  "${PROFILE_ARGS[@]}"

aws cloudformation deploy \
  --template-file infra/packaged.yaml \
  --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "${PARAMS[@]}" \
  "${PROFILE_ARGS[@]}" --region "$REGION"

# 5. Show outputs.
aws cloudformation describe-stacks --stack-name "$STACK" "${PROFILE_ARGS[@]}" --region "$REGION" \
  --query "Stacks[0].Outputs" --output table

if [ "$DEPLOY_WEB_HOST" = "true" ]; then
  cat <<'NOTE'

Backend produces feed.json on its schedule (or invoke the function once to populate now).
Front-end host is up; finish the gate (builder-provisioned):
  - Register WebInstanceId in an ALB target group on the app port, health check '/'.
  - Add the HTTPS:443 listener rule: host-header -> authenticate (OIDC) -> forward.
  - Point DNS at the ALB.
  - Store the real Slack webhook URL in the SlackSecret (see DEPLOYMENT.md).

To push front-end changes later: re-run this script to refresh the bundle, then
re-bootstrap the host (SSM run-command re-running UserData, or replace the instance).
NOTE
else
  cat <<'NOTE'

Backend-only (A2-static) deploy complete. The backend produces feed.json on its
schedule (or invoke the function once to populate now). The front-end serves as
static files from the shared landing host (tools.inquirer.com/weather/), which syncs
feed.json from the SiteBucket via the weather-feed-sync timer (see the deploy runbook).
Store the real Slack webhook URL in the SlackSecret (output: SlackSecretArn).
NOTE
fi
