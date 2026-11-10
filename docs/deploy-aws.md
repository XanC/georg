# Deploying Georg on AWS (stop-when-idle)

This is the runbook for the single-instance deployment in `deploy/aws/` (architecture §7).
One EC2 instance runs Docker Compose (`deploy/compose.yaml`): `georg-web`, `georg-worker`,
`caddy` (automatic HTTPS) and `litestream` (SQLite → S3). It powers itself off when idle.
CloudFront answers `georg.stpaulaustin.org` whether the instance is running or not: the
instance is the primary origin and a wake-page Lambda is the failover origin, so the
browser only ever sees that one name.

| Piece | Where |
| --- | --- |
| CloudFormation stack | `deploy/aws/georg.yaml` (wrapper: `deploy/aws/deploy.sh`) |
| ACM certificate for CloudFront (us-east-1) | `deploy/aws/cloudfront-certificate.yaml` |
| Boot: data volume, secrets, DNS | `deploy/aws/boot/`, `deploy/aws/systemd/` |
| Wake origin (Lambda function URL) | `deploy/aws/wake/index.py`, inlined in the template |
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

Source videos are never downloaded: the worker streams them straight from Google Drive
and only the extracted audio (about 1 GB per service) and the stored artifacts take
space, so the default 50 GB data volume fits with plenty of headroom.

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
   `Hostname` (`georg.stpaulaustin.org`), `OriginHostname` (`origin.georg.stpaulaustin.org`),
   `HostedZoneId` (empty = no DNS updates), `CertificateArn` (empty = no CloudFront),
   `WakeSchedule` (`cron(0 17-21 ? * SUN *)`: hourly 17:00–21:00, so a recording uploaded late is still found) and `WakeTimezone` (`America/Chicago`),
   `WakeScheduleState`, `SnapshotSchedule`, `SnapshotRetainCount`, `GitRef`, `SesIdentity`.
   Set `REGION`/`STACK` in the environment to change the region (default `us-east-2`) or
   stack name (default `georg`). Read the outputs with

   ```sh
   aws cloudformation describe-stacks --region us-east-2 --stack-name georg \
     --query 'Stacks[0].Outputs' --output table
   ```

4. **DNS and CloudFront** — see the next section. With `CertificateArn` set the stack
   creates a CloudFront distribution whose alias is `Hostname`, whose primary origin is
   `OriginHostname` on the instance, and whose failover origin is the wake page. The
   instance keeps the `OriginHostname` A record in `HostedZoneId` pointing at itself
   (no Elastic IP, so nothing is billed while stopped; while stopped the record points
   at a released IP, and the TLS handshake or connection failure is exactly what makes
   CloudFront fail over).

5. **Configuration.** Connect with Session Manager (no SSH port is open):

   ```sh
   aws ssm start-session --region us-east-2 --target i-…
   sudo cp /opt/georg/deploy/georg.aws.toml /srv/georg/config/georg.toml
   sudo editor /srv/georg/config/georg.toml   # set outputs, email, review.base_url
   sudo systemctl start georg.service georg-discover.timer
   ```

   The first boot builds the image with the `classify`, `whisper`, `llm` and `gdrive` extras
   (several minutes; `GEORG_EXTRAS` in `deploy/compose.yaml` selects them).

6. **Check.** `curl https://georg.stpaulaustin.org/healthz` (through CloudFront) while
   the instance is running, and open `https://georg.stpaulaustin.org/` in a browser
   while it is stopped — the wake page should answer, start the instance, and keep
   reloading until Georg is up.

## DNS and CloudFront

Two names are involved:

- `Hostname` (`georg.stpaulaustin.org`): what browsers use. It points at the CloudFront
  distribution, never at the instance.
- `OriginHostname` (`origin.georg.stpaulaustin.org`): how CloudFront reaches the
  instance, and the only name Caddy serves. It gets a Let's Encrypt certificate
  for it (HTTP-01, so the security group stays open to the world — restricting it
  to CloudFront's prefix list would break certificate issuance). The boot script
  UPSERTs its A record at every boot. Caddy does not serve `Hostname`: CloudFront
  rewrites `Host` to the origin name, so a certificate for the public name could
  never be issued — asking for one would just produce failing ACME retries.

DNS for `stpaulaustin.org` lives in Cloudflare. Setup, by hand:

1. **Create a Route 53 hosted zone for `OriginHostname`** (`origin.georg.stpaulaustin.org`)
   and pass its id as `HostedZoneId`. In Cloudflare, delegate it: one NS record named
   `origin.georg` for each of the four name servers the zone lists (DNS only, as NS
   always is).
2. **Create the ACM certificate in us-east-1** (CloudFront only accepts us-east-1
   certificates):

   ```sh
   aws cloudformation deploy --region us-east-1 --stack-name georg-cert \
     --template-file deploy/aws/cloudfront-certificate.yaml \
     --parameter-overrides Hostname=georg.stpaulaustin.org
   aws acm describe-certificate --region us-east-1 \
     --certificate-arn <CertificateArn output> \
     --query 'Certificate.DomainValidationOptions[0].ResourceRecord'
   ```

   Add the reported `Name`/`Value` as a DNS-only CNAME in Cloudflare and wait for the
   certificate status to become `ISSUED` (usually a few minutes).
3. **Point `georg` at the distribution** (the `DistributionDomainName` stack output):
   a DNS-only CNAME named `georg` → `d…….cloudfront.net` in Cloudflare. Proxied would
   also work but adds nothing; DNS-only keeps behaviour predictable.

Then rerun `deploy/aws/deploy.sh` with `HostedZoneId` and `CertificateArn`.

Without `CertificateArn` the stack creates no distribution; point `Hostname` at the
instance directly, set `OriginHostname` to that same name, and review links still
work but nothing answers while the instance is stopped except the wake page's own
URL (`WakeUrl` output).

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
put /georg/files/sftp-known-hosts "$(ssh-keyscan -t ed25519 ssh.example.org)"   # the outputs' key_path/known_hosts

# Hosted transcription/formatting, if used.
put /georg/OPENAI_API_KEY …
```

Non-secret values may use `--type String`. Type the values in a shell with history
disabled or read them from files; never commit them.

### Email (SES)

Georg emails the `review.notify` addresses through `review.smtp`:

- **Ready for review** when processing proposes a sermon revision.
- **Job failed** (subject `Georg: <kind> failed for <service date>`) when a discover,
  process, render, notify, or publish job fails for good: a non-retryable error, its
  retries exhausted, or its lease expired with no attempts left. The body has the error,
  the service, each output's publication status for publish jobs, the number of
  attempts, and a link to the service (or the jobs page) when `review.base_url` is set.
  A revoked Drive sign-in shows up here with the hint to run `georg drive-login`.
  Failures that will be retried send nothing. Each failed job is emailed at most once,
  even across worker restarts (`jobs.failure_notified_at`). URL query strings and user
  info, bearer tokens, secret-looking `key=value` pairs, and the values of `env:`
  settings are removed from the text. If sending fails, the worker logs it and carries
  on.

Without `notify` addresses or `smtp` settings, no email is sent; failed jobs are still
marked as reported, so turning email on later sends no backlog. With SES in `us-east-2`:

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

- on the schedule (`WakeSchedule`, `WakeTimezone`; default hourly 17:00–21:00 on Sundays,
  Chicago time, after the morning services are uploaded);
- when someone opens `https://georg.stpaulaustin.org/` while it is stopped: CloudFront
  fails over (GET/HEAD/OPTIONS only) to the wake page, which starts the instance, shows
  "Starting Georg; this takes about a minute" and reloads the same URL until the
  instance origin answers;
- manually: `aws ec2 start-instances --region us-east-2 --instance-ids i-…`.

POST forms in the review UI do not fail over (CloudFront only fails over GET, HEAD and
OPTIONS), so a submit while the instance is stopped shows an error; submit again once
the wake page has brought it up.

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

After upgrading a processor (e.g. a transcriber or boundary-detector fix), a service
that already has a proposal can be run again with `georg reprocess SERVICE_ID` on the
instance (inside the container: `docker compose exec georg georg reprocess SERVICE_ID`),
or with the **Reprocess** button on the sermon's review page, which only admins see.
Either queues a `process` job with `reprocess: true`; the stage cache reuses unchanged
stages (extracted audio, classification), so no re-download happens, and the fresh
proposal becomes the current revision while previous revisions — including manual
edits — stay restorable. Reprocessing is refused once the sermon has a publication
record.

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
| CloudFront + ACM certificate | ≈ $0.09/GB + pennies/10k requests | same |
| Cloudflare DNS | — | $0 (free plan) |

A few hours of processing per week plus review sessions comes to well under $15/month;
always-on would add ≈ $60/month for the instance alone. Check current prices before
relying on these figures.
