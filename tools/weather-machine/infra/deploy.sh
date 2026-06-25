#!/usr/bin/env bash
# Deploy / update the Philadelphia Weather Machine (fixit) stack.
# AWS CLI only (no SAM, no Docker) - the Lambda has no third-party deps.
# Run from this tool's directory:  cd tools/weather-machine && bash infra/deploy.sh
#
# Network inputs are builder-provisioned and passed via environment (no personal
# defaults baked in). The ALB, Okta OIDC app, target group, listener rule, and
# DNS are created OUT OF BAND - see the builder spec in DEPLOYMENT.md.
set -euo pipefail

PROFILE="${AWS_PROFILE:-default}"
REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK_NAME:-weather-machine}"
CONTACT="${CONTACT_EMAIL:-weather-machine@inquirer.com}"
SITE_URL="${SITE_URL:-}"

# Required network inputs (fail fast if unset).
: "${VPC_ID:?set VPC_ID (the VPC for the front-end host)}"
: "${SUBNET_ID:?set SUBNET_ID (a subnet the ALB can reach)}"
: "${ALB_SG_ID:?set ALB_SG_ID (the ALB security group id)}"

ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
DEPLOY_BUCKET="${DEPLOY_BUCKET:-weather-machine-deploy-${ACCOUNT}}"
ASSETS_KEY="web-assets.tar.gz"

echo "Account: $ACCOUNT | Profile: $PROFILE | Region: $REGION | Stack: $STACK"

# 1. One-time deploy bucket for the Lambda zip + the web bundle.
if ! aws s3api head-bucket --bucket "$DEPLOY_BUCKET" --profile "$PROFILE" 2>/dev/null; then
  echo "Creating deploy bucket $DEPLOY_BUCKET ..."
  aws s3api create-bucket --bucket "$DEPLOY_BUCKET" --profile "$PROFILE" --region "$REGION"
fi

# 2. Lambda source dir (only the two files the function needs).
rm -rf build && mkdir build
cp pipeline/lambda_function.py pipeline/rewrite_common.py build/

# 3. Front-end bundle: site/ + feed-sync.sh + systemd units. The EC2 UserData
#    pulls and unpacks this at boot into /opt/weather-machine.
rm -rf .assets && mkdir -p .assets/site
cp -r site/. .assets/site/
cp infra/feed-sync.sh .assets/
cp -r infra/systemd .assets/systemd
tar -czf web-assets.tar.gz -C .assets .
aws s3 cp web-assets.tar.gz "s3://${DEPLOY_BUCKET}/${ASSETS_KEY}" \
  --profile "$PROFILE" --region "$REGION"

# 4. Package (zip + upload the Lambda) and deploy the stack.
aws cloudformation package \
  --template-file infra/template.yaml \
  --s3-bucket "$DEPLOY_BUCKET" \
  --output-template-file infra/packaged.yaml \
  --profile "$PROFILE"

aws cloudformation deploy \
  --template-file infra/packaged.yaml \
  --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    "ContactEmail=${CONTACT}" \
    "SiteUrl=${SITE_URL}" \
    "VpcId=${VPC_ID}" \
    "SubnetId=${SUBNET_ID}" \
    "AlbSecurityGroupId=${ALB_SG_ID}" \
    "AssetsBucket=${DEPLOY_BUCKET}" \
    "AssetsKey=${ASSETS_KEY}" \
  --profile "$PROFILE" --region "$REGION"

# 5. Show outputs (and the remaining builder-provisioned steps).
aws cloudformation describe-stacks --stack-name "$STACK" --profile "$PROFILE" --region "$REGION" \
  --query "Stacks[0].Outputs" --output table

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
