# Deployment — Philadelphia Weather Machine (fixit)

A step-by-step runbook to stand up the tool as **Pattern C**: a Pattern A
Okta-gated EC2 front-end reading `feed.json` from S3, fed by a Pattern B
EventBridge → Lambda(+Bedrock) backend.

Each step is tagged:

- **[BUILDER]** — provisioned by the operator (network/identity boundary). This
  repo cannot create these; it gives you the exact spec and commands.
- **[CODE]** — handled by this repo (`infra/deploy.sh` + the CloudFormation
  template).

Steps are ordered the way the platform teaches a request — **identity/network
boundary → app → secret → verify**. A few boundary pieces (target group,
listener, DNS) are finished *after* the stack deploy because they reference the
EC2 instance it creates; that ordering is called out inline.

---

## 0. Prerequisites

- AWS CLI v2 configured for the **org** account (`export AWS_PROFILE=<org-profile>`).
  Do **not** use a personal profile or root.
- An existing **VPC** and a **subnet** the ALB can route to.
- Okta admin access (or your IdP team) to create an OIDC app + access group.
- Region: **us-east-1** (Bedrock model + the stack default).
- `bash`, `tar`, and this repo checked out locally.

Set the values you'll reuse:

```bash
export AWS_PROFILE=<org-profile>
export AWS_REGION=us-east-1
export STACK_NAME=weather-machine
export HOSTNAME=weather-machine.intra.inquirer.com   # the internal hostname you'll gate
```

---

## 1. [BUILDER] Enable the Bedrock model (one-time)

In the **Bedrock console (us-east-1)** → Model access, enable
**Claude Sonnet 4.6** and submit the Anthropic use-case details form. Until this
is granted, `bedrock:InvokeModel` is rejected and rewrites fall back to official
text (the app still works; it just won't simplify).

---

## 2. [BUILDER] Create the Okta OIDC app + access group

In the Okta admin console:

1. **Applications → Create App Integration → OIDC → Web Application.**
2. Sign-in redirect URI: `https://<HOSTNAME>/oauth2/idpresponse`
   (the ALB's fixed OIDC callback path).
3. Capture **Client ID**, **Client secret**, and your Okta **issuer**
   (`https://<org>.okta.com`).
4. Create/assign an **access group** (e.g. `newsroom-weather-machine`) and add the
   newsroom users. Assign the app to that group — this group *is* the door.

These three OIDC values feed the ALB listener rule in step 6.

---

## 3. [BUILDER] Create the ALB and its security group

The host security group the stack creates references the **ALB's** security
group, so the ALB SG must exist first.

```bash
# ALB security group (HTTPS in from the internal network / VPN range as per org policy)
ALB_SG_ID=$(aws ec2 create-security-group \
  --group-name weather-machine-alb --description "WM ALB" \
  --vpc-id <VPC_ID> --query GroupId --output text)

# Internet-facing or internal per your org's ALB convention; HTTPS:443 listener added in step 6.
ALB_ARN=$(aws elbv2 create-load-balancer --name weather-machine-alb \
  --type application --subnets <SUBNET_A> <SUBNET_B> \
  --security-groups "$ALB_SG_ID" \
  --scheme internal \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)
```

Note the **ALB_SG_ID** — it's a required input to the stack.

---

## 4. [CODE] Deploy the stack

This creates the private S3 bucket, DynamoDB cache, the Bedrock Lambda + scoped
role, the 3-minute schedule, the empty **Secrets Manager** secret, and the
**EC2 front-end host** with its scoped instance role and its security group
(which accepts the app port **only from the ALB SG**).

```bash
export VPC_ID=<VPC_ID>
export SUBNET_ID=<SUBNET_ID>          # a subnet the ALB reaches
export ALB_SG_ID=<ALB_SG_ID>          # from step 3
export SITE_URL="https://${HOSTNAME}" # used only in Slack links

bash infra/deploy.sh
```

When it finishes, note the stack outputs — you'll need **WebInstanceId**,
**WebAppPort** (default 8080), **SlackSecretArn**, and **SiteBucket**:

```bash
aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs" --output table
```

The EC2 host bootstraps itself via UserData: it pulls the front-end bundle,
installs the `systemd` units, and starts `weather-machine-web` (the static
server) and the `feed-sync` timer. It is **SSM-managed — no SSH key pair, no
inbound 22.** Confirm it registered with SSM:

```bash
aws ssm describe-instance-information \
  --query "InstanceInformationList[?InstanceId=='<WebInstanceId>']"
```

---

## 5. [BUILDER] Target group → register the host

```bash
TG_ARN=$(aws elbv2 create-target-group --name weather-machine-tg \
  --protocol HTTP --port 8080 --vpc-id "$VPC_ID" --target-type instance \
  --health-check-protocol HTTP --health-check-path / \
  --query 'TargetGroups[0].TargetGroupArn' --output text)

aws elbv2 register-targets --target-group-arn "$TG_ARN" \
  --targets Id=<WebInstanceId>,Port=8080

# Wait for healthy (the health check hits '/', which serves index.html):
aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
  --query 'TargetHealthDescriptions[].TargetHealth.State'
```

---

## 6. [BUILDER] HTTPS listener → authenticate (Okta) → forward

This is the gate. The HTTPS:443 listener authenticates with Okta **before**
forwarding to the target group, so an unauthenticated request never reaches the
app.

```bash
# 443 listener (needs an ACM cert for the hostname):
LISTENER_ARN=$(aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" \
  --protocol HTTPS --port 443 \
  --certificates CertificateArn=<ACM_CERT_ARN> \
  --default-actions Type=fixed-response,FixedResponseConfig='{StatusCode=403,ContentType=text/plain,MessageBody=Forbidden}' \
  --query 'Listeners[0].ListenerArn' --output text)

# Rule: host-header match -> authenticate-oidc (Okta) -> forward to the target group.
aws elbv2 create-rule --listener-arn "$LISTENER_ARN" --priority 10 \
  --conditions Field=host-header,Values="$HOSTNAME" \
  --actions '[
    {
      "Type":"authenticate-oidc","Order":1,
      "AuthenticateOidcConfig":{
        "Issuer":"https://<org>.okta.com",
        "AuthorizationEndpoint":"https://<org>.okta.com/oauth2/v1/authorize",
        "TokenEndpoint":"https://<org>.okta.com/oauth2/v1/token",
        "UserInfoEndpoint":"https://<org>.okta.com/oauth2/v1/userinfo",
        "ClientId":"<OKTA_CLIENT_ID>",
        "ClientSecret":"<OKTA_CLIENT_SECRET>",
        "OnUnauthenticatedRequest":"authenticate"
      }
    },
    {"Type":"forward","Order":2,"TargetGroupArn":"'"$TG_ARN"'"}
  ]'
```

---

## 7. [BUILDER] DNS → ALB

Point the internal hostname at the ALB (Route 53 alias):

```bash
# A-record alias <HOSTNAME> -> <ALB DNS name>. Example via change-resource-record-sets,
# or in the console: Alias = Yes, target = the ALB.
aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].DNSName' --output text
```

---

## 8. [BUILDER] Store the Slack webhook in Secrets Manager

The stack created the secret empty so the Lambda starts without it (it just
skips Slack). Put the real webhook URL in when ready — it never touches source:

```bash
aws secretsmanager put-secret-value \
  --secret-id <SlackSecretArn> \
  --secret-string "https://hooks.slack.com/services/XXX/YYY/ZZZ"
```

---

## 9. Seed and verify

**Backend** — invoke once so `feed.json` exists immediately (or wait 3 min):

```bash
aws lambda invoke --function-name <FunctionName> /tmp/out.json && cat /tmp/out.json
# Confirm the feed landed in the private bucket:
aws s3 ls "s3://<SiteBucket>/feed.json"
```

**Slack wiring** (optional self-test, no storm needed):

```bash
aws lambda invoke --function-name <FunctionName> \
  --cli-binary-format raw-in-base64-out --payload '{"slack_test": true}' /tmp/o.json && cat /tmp/o.json
```

**Front-end / gate** — browse to `https://<HOSTNAME>`. You should be bounced to
Okta, and after login see the live page. Confirm the boundary holds:

- Hitting the EC2 host's app port directly (not via the ALB) must **fail** —
  the host SG only allows the ALB SG. That non-route is the whole point.
- The feed refreshes: the `feed-sync` timer pulls `feed.json` every minute.

```bash
# On the host (via SSM Session Manager, since there's no SSH):
systemctl status weather-machine-web.service feed-sync.timer --no-pager
journalctl -u feed-sync.service -n 20 --no-pager
```

---

## Updating later

- **Backend / Lambda or front-end files:** re-run `bash infra/deploy.sh`. It
  refreshes the Lambda and re-uploads the web bundle. Front-end file changes
  need the host to re-bootstrap — either re-run UserData via SSM Run Command, or
  terminate the instance and let CloudFormation replace it (the timer + service
  come back automatically; nothing on the box is stateful).
- **`feed.json`:** nothing to do — the backend rewrites and the timer syncs.

## Teardown

```bash
# Empty the buckets first, then:
aws cloudformation delete-stack --stack-name "$STACK_NAME"
# Remove the builder-provisioned ALB, target group, listener, Okta app, and DNS record separately.
```

---

## Builder spec (quick reference)

| Item | Value |
|------|-------|
| Okta | OIDC web app + access group `newsroom-weather-machine`; redirect `https://<HOSTNAME>/oauth2/idpresponse` |
| Host SG | app port (8080) from **ALB SG by id** only — created by the stack |
| Target group | `instance:8080`, health check path `/` |
| Listener rule | HTTPS:443, host-header `<HOSTNAME>` → authenticate-oidc → forward |
| DNS | alias `<HOSTNAME>` → ALB |
| Secret | Slack webhook URL → Secrets Manager `weather-machine/slack-webhook` |

*The Inquirer · IT / Systems · Confidential — internal*
