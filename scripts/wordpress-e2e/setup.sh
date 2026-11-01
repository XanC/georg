#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Starts a throwaway WordPress + Church Content site with georg-bridge active, and creates
# users for scripts/wordpress-e2e/e2e.py. Writes their application passwords to .env.
set -euo pipefail
cd "$(dirname "$0")"
port="${GEORG_WP_PORT:-8080}"
wp() { docker compose --progress quiet run --rm -T cli wp "$@"; }

docker compose --progress quiet up -d --wait
for _ in $(seq 60); do
  curl -fsS -o /dev/null "http://localhost:$port/wp-admin/install.php" && break
  sleep 1
done
if ! wp core is-installed 2>/dev/null; then
  wp core install --url="http://localhost:$port" --title="Georg e2e" \
    --admin_user=admin --admin_password=admin --admin_email=admin@example.invalid --skip-email
fi
wp rewrite structure '/%postname%/'
wp plugin install church-theme-content --activate
wp plugin activate georg-bridge

new_user() { # login role -> application password
  wp user get "$1" --field=ID >/dev/null 2>&1 \
    || wp user create "$1" "$1@example.invalid" --role="$2" --user_pass="$1" >/dev/null
  wp user application-password delete "$1" --all >/dev/null 2>&1 || true
  wp user application-password create "$1" georg-e2e --porcelain
}
{
  echo "GEORG_WP_URL=http://localhost:$port"
  echo "GEORG_WP_EDITOR_PASSWORD=$(new_user georg editor)"
  echo "GEORG_WP_AUTHOR_PASSWORD=$(new_user author1 author)"
  echo "GEORG_WP_SUBSCRIBER_PASSWORD=$(new_user subscriber1 subscriber)"
} > .env.e2e
echo "ready: http://localhost:$port (admin/admin); credentials in $(pwd)/.env.e2e"
