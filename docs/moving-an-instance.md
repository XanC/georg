# Moving an instance to another host

A church's Georg instance moves between deployments as a single `.georg` bundle.
Because every publication's key carries the permanent church UUID (ADR 0003) and
publication history travels with the database, the new host updates the objects
the old host created — never duplicates them.

## On the old host

```sh
georg export --out stpaul.georg
```

This writes a tar containing:

- `manifest.json` — bundle format version, Georg version, Alembic revision,
  church UUID, creation time, the SHA-256 of every bundled file, and the list of
  deployment-specific paths found in the database;
- `db.sqlite3` — a consistent database snapshot (SQLite backup API), with every
  absolute filesystem path replaced by a token the import remaps;
- `artifacts/<sha256>` — every artifact file still present in the store.

**Exporting freezes the source.** `instance_identity.state` becomes `frozen`:
discovery and publishing there stop for good (the worker's `discover`/`publish`
jobs and the web approve/publish endpoints all refuse it), so two deployments
can never act on the same Drive folder or website. The freeze happens before
the snapshot, after running jobs drain, so a publication committed mid-export
is still in the bundle. A frozen instance may export again (recovery from a
failed export) but can never resume.

The bundle contains **no secrets and no `georg.toml`** — those never leave the
source host. The manifest lists filesystem paths (input roots, model caches,
key files) it could not make relative; they are informational.

## On the new host

Write the new host's `georg.toml` (new `data_dir`, inputs and outputs for this
deployment) and provide its secrets as environment variables. Then, into an
empty data dir:

```sh
georg import stpaul.georg --config /etc/georg/georg.toml
```

Import refuses to run if a database already exists, verifies every file's
checksum, and refuses a bundle written by a newer Georg (a bundle from an older
one is migrated forward). It restores the database — church UUID, services,
revisions, approvals and publication rows included — remaps paths onto the new
`data_dir`, drops the copied job queue (work is re-derived), and restores the
artifacts into the new store.

The imported instance starts **paused**: discovery and publishing stay off.

```sh
georg resume
```

Before unpausing, `georg resume` calls each published publication's target
`find_existing(publication_key)` and reports objects missing at the target,
targets that can't be reached, and publications whose target is no longer
configured. Any problem refuses the resume unless `--force` is passed. After a
clean resume the instance is `active` and works normally.
