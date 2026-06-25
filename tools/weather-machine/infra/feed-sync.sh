#!/usr/bin/env bash
# Pull the latest feed.json produced by the Lambda backend from S3 into the
# web docroot, served same-origin so the page's fetch('feed.json') is unchanged.
# Written atomically (download to a temp file, then mv) so the web server never
# serves a half-written feed. Run on a 1-minute systemd timer.
set -euo pipefail

: "${SITE_BUCKET:?SITE_BUCKET must be set (see /etc/weather-machine/web.env)}"
REGION="${AWS_REGION:-us-east-1}"
DOCROOT="${WM_DOCROOT:-/opt/weather-machine/site}"
KEY="${FEED_KEY:-feed.json}"

tmp="${DOCROOT}/.${KEY}.tmp"
aws s3 cp "s3://${SITE_BUCKET}/${KEY}" "${tmp}" --region "${REGION}" --only-show-errors
mv -f "${tmp}" "${DOCROOT}/${KEY}"
echo "feed-sync: updated ${DOCROOT}/${KEY} from s3://${SITE_BUCKET}/${KEY}"
