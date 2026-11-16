# 0003. One Georg instance per church, identified by a permanent UUID

**Status:** Accepted

## Context

Georg will run for many churches. It needs a deployment model, a stable
identity that publication keys can carry, and a way to move an instance between
deployments. The one existing deployment (St. Paul) already has publications
keyed `georg:{instance-slug}:...` in its database and at its outputs.

## Decision

- **Church = instance.** Each church gets its own Georg deployment: its own
  `georg.toml`, SQLite database, and data directory. Isolation between churches
  is isolation between deployments — there is no shared tenancy inside one
  Georg.
- **Hosting runs many instances side by side.** No shared compute pool for now.
  Two rules keep a future pool possible: processors stay DB-free (files/text
  in, results out — already an AGENTS.md rule), and stage cache keys include
  provider and model identity so local and pooled results never mix.
- **A permanent church UUID identifies the instance.** A single-row
  `instance_identity` table holds a `church_id` UUID generated once (by the
  migration that creates the table, or on first read) and never regenerated.
  Publication keys are `georg:{church_id}:sermon-{sermon_id}:{target}`; the
  human-readable `instance` slug in `georg.toml` becomes display-only and can
  be renamed freely. Export/import carries the row; a fresh install generates
  a new one.
- **No compatibility code for pre-UUID keys.** The single existing instance is
  migrated once by `georg migrate-publication-keys`: it emits a WP-CLI script
  for the WordPress meta, re-keys Google Drive `appProperties`, and updates the
  `publications` rows.
- **Archives are outputs, not Georg's job to keep.** Long-term preservation is
  an output to storage the church or the hosting provider owns (e.g. an S3
  archive target); Georg's own data directory is working storage with a
  retention limit, not an archive.
- **Export/import moves instances.** A planned `georg export` / `georg import`
  bundle (DB snapshot, effective settings minus secrets, optional artifacts)
  moves a church between deployments without duplicating publications.
- **Releases and upgrades** (planned): versioned images published to GHCR,
  a pre-migration database backup, refusing to start on a newer database, and
  an on-host `georg upgrade` replacing laptop deploy scripts.

## Consequences

- `publication_key()` needs the church UUID, which lives in the database, so
  callers pass it in from `IdentityRepository` rather than reading
  `config.instance`.
- The key form is the only change outputs see; targets that store the key
  (WordPress post/attachment meta, Drive `appProperties`) find their objects by
  the new key after the one-time migration.
