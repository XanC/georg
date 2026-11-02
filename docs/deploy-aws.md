# Deploying Georg on AWS (stop-when-idle)

This is the runbook for the single-instance deployment in `deploy/aws/` (architecture §7).
One EC2 instance runs Docker Compose (`deploy/compose.yaml`): `georg-web`, `georg-worker`,
`caddy` (automatic HTTPS) and `litestream` (SQLite → S3). It powers itself off when idle and
is started by a weekly schedule or by opening the wake page.

| Piece | Where |
| --- | --- |
| CloudFormation stack | `deploy/aws/georg.yaml` (wrapper: `deploy/aws/deploy.sh`) |
| Boot: data volume, secrets, DNS | `deploy/aws/boot/`, `deploy/aws/systemd/` |
| Wake page (Lambda function URL) | `deploy/aws/wake/index.py`, inlined in the template |
| Compose, Caddy, Litestream | `deploy/compose.yaml`, `deploy/Caddyfile`, `deploy/litestream.yml` |
| Georg config for the host | `deploy/georg.aws.toml` → `/srv/georg/config/georg.toml` |

Commands below use the AWS CLI v2 with credentials for the Georg account, region `us-east-2`.

## Layout on the instance

| Path | What | Persistent |
| --- | --- | --- |
| `/srv/georg` | gp3 data volume (label `georg-data`), snapshotted by DLM | yes |
| `/srv/georg/data` | Georg `data_dir`: `georg.sqlite3` and artifacts | yes |
| `/srv/georg/config/georg.toml` | Georg configuration (no secrets) | yes |
| `/srv/georg/caddy` | TLS certificates and ACME account | yes |
| `/run/georg/georg.env` | secrets from SSM, mode 0600 | tmpfs, rewritten every boot |
| `/run/georg/secrets/` | secret files from SSM, mode 0600 | tmpfs, rewritten every boot |
| `/run/georg/host.env` | non-secret Compose settings (hostname, bucket, region) | tmpfs |
| `/opt/georg` | git checkout used to build the image | root volume |

Every boot `georg-boot.service` reads all parameters under `/georg/` from SSM Parameter
Store, writes them to `/run/georg`, updates the Route 53 record (if configured), and then
`georg.service` runs `docker compose up`. Logs: `journalctl -u georg-boot -u georg`.

## First deployment

1. **Pick inputs.** A VPC and a public subnet (the default VPC is fine) and the current
   Ubuntu 24.04 AMI:

   ```sh
   aws ssm get-parameter --region us-east-2 \
     --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
     --query Parameter.Value --output text
   ```

2. **Create the secrets** (next section). They can also be added after the stack exists;
   reboot the instance afterwards.

3. **Create the stack:**

   ```sh
   deploy/aws/deploy.sh VpcId=vpc-… SubnetId=subnet-… ImageId=ami-…
   ```

   Optional parameters: `InstanceType` (default `t3.large`), `DataVolumeSize` (50),
   `Hostname` (`georg.stpaulaustin.org`), `HostedZoneId` (empty = no DNS updates),
   `WakeSchedule` (`cron(0 17-21 ? * SUN *)`: hourly 17:00–21:00, so a recording uploaded late is still found) and `WakeTimezone` (`America/Chicago`),
   `WakeScheduleState`, `SnapshotSchedule`, `SnapshotRetainCount`, `GitRef`, `SesIdentity`.
   Set `REGION`/`STACK` in the environment to change the region (default `us-east-2`) or
   stack name (default `georg`). Read the outputs with

   ```sh
   aws cloudformation describe-stacks --region us-east-2 --stack-name georg \
     --query 'Stacks[0].Outputs' --output table
   ```

4. **DNS.** With `HostedZoneId` set, the instance UPSERTs an A record for `Hostname` at
   every boot (TTL 60; no Elastic IP, so nothing is billed while stopped). Without a
   hosted zone, point the name at the instance's public IP yourself — it changes at each
   start, so a Route 53 zone (or delegating a subdomain such as `georg.stpaulaustin.org`
   to one) is strongly recommended. Caddy obtains the certificate once the name resolves.

5. **Configuration.** Connect with Session Manager (no SSH port is open):

   ```sh
   aws ssm start-session --region us-east-2 --target i-…
   sudo cp /opt/georg/deploy/georg.aws.toml /srv/georg/config/georg.toml
   sudo editor /srv/georg/config/georg.toml   # set review.base_url to the WakeUrl output
   sudo systemctl start georg.service georg-discover.timer
   ```

   The first boot builds the image with the `classify`, `whisper`, `llm` and `gdrive` extras
   (several minutes; `GEORG_EXTRAS` in `deploy/compose.yaml` selects them).

6. **Check.** `curl https://georg.stpaulaustin.org/healthz` and open the `WakeUrl` output
   in a browser while the instance is stopped.

## Secrets (SSM Parameter Store)

All secrets live under `/georg/` as `SecureString` parameters. Each
`/georg/NAME` becomes the environment variable `NAME` for the web and worker containers,
which the config references as `"env:NAME"`. Each `/georg/files/NAME` becomes the file
`/run/secrets/NAME` inside the containers. Values may not contain newlines or single
quotes unless stored as a file. Only parameter names are logged.

```sh
put() { aws ssm put-parameter --region us-east-2 --type SecureString --overwrite --name "$1" --value "$2"; }

# Session signing key for the web UI.
put /georg/GEORG_SECRET_KEY "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

# georg-bridge HMAC key; the same value goes into the site's wp-config.php (docs/wordpress.md).
put /georg/GEORG_WP_KEY "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

# Google credentials (Drive input and archive output) as a file: the output of
# `georg drive-login` or a service account key (docs/google-drive.md).
put /georg/files/google-credentials.json "$(cat drive-credentials.json)"
put /georg/GOOGLE_APPLICATION_CREDENTIALS /run/secrets/google-credentials.json
put /georg/GEORG_DRIVE_FOLDER_ID …
put /georg/GEORG_ARCHIVE_FOLDER_ID …

# SFTP key for the phone output, as a file.
put /georg/files/sftp-key "$(cat id_ed25519)"
put /georg/GEORG_SFTP_KEY_FILE /run/secrets/sftp-key

# Hosted transcription/formatting, if used.
put /georg/OPENAI_API_KEY …
```

Non-secret values may use `--type String`. Type the values in a shell with history
disabled or read them from files; never commit them.

### Email (SES)

Review notifications use SMTP. With SES in `us-east-2`:

1. Verify the sending domain (or address) in SES and, while the account is in the SES
   sandbox, each recipient — or request production access.
2. Create SES SMTP credentials (SES console → SMTP settings; this creates an IAM user)
   and store them:

   ```sh
   put /georg/GEORG_SMTP_USER AKIA…
   put /georg/GEORG_SMTP_PASSWORD …
   ```

3. Keep `review.smtp.host = "email-smtp.us-east-2.amazonaws.com"` in `georg.toml`.

The instance role also allows `ses:SendEmail`/`ses:SendRawEmail` for `SesIdentity`, for
sending through the SES API instead of SMTP credentials.

After changing secrets, reboot (`sudo reboot`) or run
`sudo systemctl restart georg-boot georg`.

## Stop-when-idle

`deploy/georg.aws.toml` sets

```toml
idle_shutdown_minutes = 10
idle_shutdown_command = ["touch", "/run/georg/shutdown/poweroff"]
```

The worker checks after every empty queue poll, inside a SQLite write transaction so that
several workers agree and none fires while a job is queued or running. Web requests other
than `/healthz` count as activity. The container cannot power off the host, so it creates
`/run/georg/shutdown/poweroff`; `georg-poweroff.path` on the host notices and runs
`systemctl poweroff`. The instance's shutdown behaviour is *stop*, so the volumes stay.

The instance starts again:

- on the schedule (`WakeSchedule`, `WakeTimezone`; default Sundays 13:00 Chicago time,
  after the morning services are uploaded);
- when someone opens the wake page (`WakeUrl` output, also used for links in review
  emails): it starts the instance, shows "Starting Georg; this takes about a minute" and
  reloads until `/healthz` answers, then redirects to the same path on the admin URL;
- manually: `aws ec2 start-instances --region us-east-2 --instance-ids i-…`.

**Always-on hosting:** remove `idle_shutdown_minutes` (and `idle_shutdown_command`) from
`/srv/georg/config/georg.toml` and `sudo systemctl restart georg`. Optionally set
`WakeScheduleState=DISABLED`. To keep the instance off temporarily, stop it; nothing
else needs to change.

## Upgrades

On the instance:

```sh
sudo git -C /opt/georg fetch
sudo git -C /opt/georg checkout <tag-or-commit>
cd /opt/georg/deploy
sudo docker compose --env-file /run/georg/host.env build
sudo systemctl restart georg
```

Database migrations run when Georg starts. If boot or systemd files changed, also run
`sudo install -m 0644 /opt/georg/deploy/aws/systemd/georg* /etc/systemd/system/ && sudo systemctl daemon-reload`.

Infrastructure changes: rerun `deploy/aws/deploy.sh` with the same parameters. Changing
`InstanceType` stops and restarts the instance. User data only runs on an instance's
first boot. Changing `ImageId` replaces the instance; the data volume and bucket are separate resources and survive, but
take a snapshot first. Deleting the stack retains the bucket and snapshots the data volume.

## Backups and restore

- **Litestream** streams `georg.sqlite3` to `s3://<BackupBucket>/litestream/georg.sqlite3`
  continuously while the instance runs (snapshots kept 30 days). The bucket is private,
  versioned and encrypted; superseded versions expire after `BackupNoncurrentDays`.
- **DLM** snapshots the data volume (database, artifacts, certificates) on
  `SnapshotSchedule` (default Mondays 12:00 UTC) and keeps `SnapshotRetainCount` (8).
  Snapshots only happen at the scheduled time; a stopped instance is fine.

**Restore the database from S3** (e.g. after corruption):

```sh
sudo systemctl stop georg
cd /srv/georg/data && sudo mv georg.sqlite3 georg.sqlite3.broken && sudo rm -f georg.sqlite3-wal georg.sqlite3-shm
cd /opt/georg/deploy
sudo docker compose --env-file /run/georg/host.env run --rm --no-deps litestream \
  restore -config /etc/litestream.yml /var/lib/georg/georg.sqlite3
sudo systemctl start georg
```

Add `-timestamp 2026-01-01T00:00:00Z` to restore an earlier point in time.

**Restore the whole volume from a snapshot:** create a gp3 volume from the snapshot in
the instance's availability zone, stop the instance, detach the current data volume,
attach the new one as `/dev/sdf`, start. The boot script mounts by label and never
formats a disk that has a filesystem. To keep CloudFormation in sync, import the new
volume or update the stack afterwards.

## Cost notes (us-east-2, on-demand, rough)

| Item | While running | While stopped |
| --- | --- | --- |
| t3.large | ≈ $0.083/h | $0 |
| Root gp3 30 GiB + data gp3 50 GiB | — | ≈ $6.40/month always |
| Public IPv4 address | ≈ $0.005/h | $0 (no Elastic IP) |
| EBS snapshots (incremental) | — | ≈ $0.05/GiB-month changed |
| S3 backups, Lambda, Scheduler, SSM standard params | cents | cents |
| Route 53 hosted zone (optional) | — | $0.50/month |

A few hours of processing per week plus review sessions comes to well under $15/month;
always-on would add ≈ $60/month for the instance alone. Check current prices before
relying on these figures.
