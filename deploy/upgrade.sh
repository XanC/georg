#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Upgrade a compose-based Georg host to a published image.
#
#   deploy/upgrade.sh 0.2.0      # exact version
#   deploy/upgrade.sh 0          # newest within major 0
#
# Pulls ghcr.io/xanc/georg:<version> ($GEORG_REGISTRY overrides the repository),
# waits for running jobs to finish (up to $GEORG_UPGRADE_TIMEOUT, default 600s),
# recreates georg-web and georg-worker, and checks the web health check. On
# failure it switches back to the previous image and prints where the
# pre-migration database backup is.
#
# Run on the host, from anywhere; it cds to its own directory. The compose env
# file defaults to /run/georg/host.env ($GEORG_ENV_FILE overrides). To pin a
# version permanently, put GEORG_IMAGE=ghcr.io/xanc/georg:<version> in that file;
# the next plain `docker compose up` keeps using it.
set -eu

VERSION=${1:?usage: upgrade.sh <version>, e.g. upgrade.sh 0.2.0 or upgrade.sh 0}
REGISTRY=${GEORG_REGISTRY:-ghcr.io/xanc/georg}
NEW_IMAGE="$REGISTRY:$VERSION"
TIMEOUT=${GEORG_UPGRADE_TIMEOUT:-600}
ENV_FILE=${GEORG_ENV_FILE:-/run/georg/host.env}
GEORG_ROOT=${GEORG_ROOT:-/srv/georg}

cd "$(dirname "$0")"

compose() { docker compose --env-file "$ENV_FILE" "$@"; }

# The image the running web container actually uses (fallback: compose default).
# Prefer the repo digest over the tag: pulling :0 moves that tag to the new image,
# so a tag would roll "back" to the very image that just failed.
cid=$(compose ps -q georg-web 2>/dev/null || true)
if [ -n "$cid" ]; then
    OLD_IMAGE=$(docker inspect -f '{{index .RepoDigests 0}}' "$cid" 2>/dev/null \
        || docker inspect -f '{{.Config.Image}}' "$cid" 2>/dev/null || true)
fi
OLD_IMAGE=${OLD_IMAGE:-georg:local}

rollback() {
    echo "upgrade to $NEW_IMAGE failed; switching back to $OLD_IMAGE" >&2
    GEORG_IMAGE=$OLD_IMAGE compose up -d georg-web georg-worker || true
    cat >&2 <<EOF
If the web check failed because a database migration did not apply, restore the
pre-migration copy written under $GEORG_ROOT/data/backups/ (newest
georg-*.sqlite), e.g.:
    sudo systemctl stop georg
    sudo mv $GEORG_ROOT/data/georg.sqlite3 $GEORG_ROOT/data/georg.sqlite3.failed
    sudo cp $GEORG_ROOT/data/backups/<newest>.sqlite $GEORG_ROOT/data/georg.sqlite3
    sudo systemctl start georg
EOF
    exit 1
}

echo "upgrading Georg to $NEW_IMAGE (was $OLD_IMAGE)"
docker pull "$NEW_IMAGE"

# wait-idle runs under the NEW image: it predates this feature on the running one,
# and it only reads the jobs table, so running the new image here migrates nothing.
echo "waiting up to ${TIMEOUT}s for running jobs to finish"
if ! GEORG_IMAGE=$NEW_IMAGE compose run --rm --no-deps -T georg-web \
    georg db wait-idle --timeout "$TIMEOUT"; then
    echo "jobs still running; try again later" >&2
    exit 1
fi

echo "restarting web and worker on $NEW_IMAGE"
# `up` exits nonzero when the health dependency fails; that's a rollback case too.
if ! GEORG_IMAGE=$NEW_IMAGE compose up -d georg-web georg-worker; then
    rollback
fi

# The container's own health check (same probe as compose.yaml).
i=0
while [ "$i" -lt 60 ]; do
    status=$(docker inspect -f '{{.State.Health.Status}}' \
        "$(compose ps -q georg-web)" 2>/dev/null || true)
    case "$status" in
        healthy) break ;;
        unhealthy) rollback ;;
    esac
    i=$((i + 1))
    sleep 5
done
[ "$i" -lt 60 ] || rollback

echo "Georg is healthy on $NEW_IMAGE"
echo "pin it with GEORG_IMAGE=$NEW_IMAGE in $ENV_FILE to survive plain 'compose up'"
