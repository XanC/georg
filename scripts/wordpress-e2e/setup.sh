#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Starts a throwaway WordPress + Church Content site with georg-bridge active, creates users
# for scripts/wordpress-e2e/e2e.py, and configures fresh georg-bridge signing keys. Writes the
# keys and the users' application passwords (used to check they are refused) to .env.e2e.
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
new_key() { python3 -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode())"; }
key1="$(new_key)"
key2="$(new_key)"
wp config set GEORG_BRIDGE_KEYS "array( 'e2e-1' => '$key1' )" --raw --type=constant >/dev/null
wp config set GEORG_BRIDGE_USER georg --type=constant >/dev/null
wp config set GEORG_BRIDGE_ALLOW_HTTP true --raw --type=constant >/dev/null  # local http only
{
  echo "GEORG_WP_URL=http://localhost:$port"
  echo "GEORG_WP_KEY_ID=e2e-1"
  echo "GEORG_WP_KEY=$key1"
  echo "GEORG_WP_KEY2=$key2"
  echo "GEORG_WP_EDITOR_PASSWORD=$(new_user georg editor)"
  echo "GEORG_WP_AUTHOR_PASSWORD=$(new_user author1 author)"
  echo "GEORG_WP_SUBSCRIBER_PASSWORD=$(new_user subscriber1 subscriber)"
} > .env.e2e
chmod 600 .env.e2e
echo "ready: http://localhost:$port (admin/admin); credentials in $(pwd)/.env.e2e"
