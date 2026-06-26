#!/usr/bin/env bash
# Pull the latest feed.json produced by the Lambda backend from S3 into the
# shared landing host's served folder, beside index.html, so the page's relative
# fetch('feed.json') resolves to /weather/feed.json. Written atomically (download
# to a temp file, then mv) so the static server never serves a half-written feed.
# Run on a 1-minute systemd timer (weather-feed-sync.timer).
set -euo pipefail

: "${SITE_BUCKET:?SITE_BUCKET must be set (see /etc/tools-landing/weather.env)}"
REGION="${AWS_REGION:-us-east-1}"
DOCROOT="${WM_DOCROOT:-/opt/tools/landing/weather}"
KEY="${FEED_KEY:-feed.json}"

tmp="${DOCROOT}/.${KEY}.tmp"
aws s3 cp "s3://${SITE_BUCKET}/${KEY}" "${tmp}" --region "${REGION}" --only-show-errors
mv -f "${tmp}" "${DOCROOT}/${KEY}"
echo "feed-sync: updated ${DOCROOT}/${KEY} from s3://${SITE_BUCKET}/${KEY}"
