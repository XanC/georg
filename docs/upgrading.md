# Releasing and upgrading Georg

## Releasing a version

Georg's version lives in two places that must agree: `pyproject.toml`
(`[project] version`) and `src/georg/__init__.py` (`__version__`).

1. Bump both to `X.Y.Z` on `main`.
2. `git tag vX.Y.Z && git push origin vX.Y.Z` — never tag anything else.
3. The `Release` workflow checks the tag matches the version, builds
   `deploy/Dockerfile` with the same extras `deploy/compose.yaml` uses, and pushes
   `ghcr.io/xanc/georg:X.Y.Z`, `:X.Y` and `:X` to GHCR.

Releasing only publishes an image; nothing updates by itself.

## Upgrading a host

Hosts that build from a checkout keep working unchanged (`image` defaults to
`georg:local`). Hosts that want published images run, on the host:

```sh
sudo /opt/georg/deploy/upgrade.sh 0.2.0   # exact version
sudo /opt/georg/deploy/upgrade.sh 0       # newest image within major 0
```

The script pulls `ghcr.io/xanc/georg:<version>` (`$GEORG_REGISTRY` overrides the
repository for private registries or testing), waits until no job is running
(`georg db wait-idle`, `$GEORG_UPGRADE_TIMEOUT` seconds, default 600), recreates
`georg-web` and `georg-worker`, and runs the same health check compose uses. On
failure it switches back to the previous image and points at the pre-migration
database backup.

To make a pin survive a plain `docker compose up` (e.g. systemd restart), put
`GEORG_IMAGE=ghcr.io/xanc/georg:0` in the compose env file. On AWS that file,
`/run/georg/host.env`, is rewritten at every boot, so put the line in
`/etc/georg/boot.env` instead; `georg-boot` copies it into `host.env`.

**Pinning:** pin to a major (`0`, `1`) to take every release in that major — the
`X` tag moves to each new `X.Y.Z` — or pin an exact `X.Y.Z` to upgrade only by
an explicit decision. Skipping releases within a major is safe: migrations run
in order, so `0.1.0` → `0.4.0` applies the intermediate revisions. Migrations
run automatically when the new containers start; just before they run, Georg
writes a consistent copy of the database to `<data_dir>/backups/` (newest 5
kept) — on AWS this sits beside the database Litestream replicates and does not
interfere with it.

**Downgrades:** a database migrated by a newer Georg is refused — the newer
revision is unknown to older code and Georg will not start. Roll forward
instead.

## Rollback

The script already rolls the image back on a failed health check. To roll back
by hand, or when the migration itself ran but the new version is broken:

```sh
sudo systemctl stop georg
sudo mv /srv/georg/data/georg.sqlite3 /srv/georg/data/georg.sqlite3.failed
sudo cp /srv/georg/data/backups/<newest georg-*.sqlite> /srv/georg/data/georg.sqlite3
GEORG_IMAGE=<previous image> sudo docker compose --env-file /run/georg/host.env \
    -f /opt/georg/deploy/compose.yaml up -d georg-web georg-worker
```

On AWS, Litestream's continuous S3 replica is a second, older fallback (see
"Backups and restore" in `deploy-aws.md`) — restore it only if the local backups
are gone, since it also contains post-upgrade writes.
