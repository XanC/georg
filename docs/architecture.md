# Georg — Architecture Proposal (v0.4)

Status: **approved in principle; v0.4** (v0.2: Google Drive input, ChurchThemes, stop-when-idle AWS; v0.3: findings from the church web server scripts, §5.6; v0.4: findings from the church processing machine, the timing POC and the earlier `stpaul-audio` effort, §5.7). Each decision in §9 becomes an ADR under `docs/adr/` when it is settled.

**Summary.** Georg is one Python application (a "modular monolith") shipped as one container image. It has two process roles from the same codebase — a FastAPI **web** process (admin API + review UI) and a **worker** process (pipeline jobs + input polling) — sharing a SQLite database and an artifact store (local filesystem first, S3 later). Inputs, processors, and outputs are ordinary Python modules behind small, typed interfaces; a core orchestrator owns all persistence, caching, retries, and state. On AWS it runs on a single EC2 instance with Docker Compose, an EBS volume, and S3 for backups; the instance is **stopped when idle** and started on a schedule or on demand. No queues, brokers, Kubernetes, or separate services.

---

## 1. Overall architecture

```
                         ┌─────────────────────────── Georg container image ───────────────────────────┐
                         │                                                                             │
  Google Drive /     ──►│  INPUTS ──► ORCHESTRATOR (pipeline + job queue) ──► PROCESSORS              │
  upload / YouTube       │                │   ▲                                 (analyze, detect,      │
                         │                │   │ results                          extract, transcribe,  │
                         │                ▼   │                                 format, metadata)     │
                         │          SQLite DB + Artifact store  ◄───────────────────┘                  │
                         │                ▲                                                            │
  Reviewer (browser) ◄──►│  WEB (FastAPI: review UI, admin API)                                        │
                         │                │ approval                                                   │
                         │                ▼                                                            │
                         │          PUBLICATION SERVICE ──► OUTPUTS ──────────────────────────────────►│──► WordPress (first)
                         └─────────────────────────────────────────────────────────────────────────────┘
```

Key structural rules:

1. **Modules are pure-ish adapters.** An input, processor, or output receives files/data and returns data. It never touches the database, the job queue, or other modules. The orchestrator wires them together and persists everything.
2. **Every stage output is an immutable, content-addressed artifact** recorded in the DB. Re-running a stage with identical inputs + parameters + module version is a cache hit.
3. **Editorial state is versioned.** Human edits and machine proposals both create new *revisions*; approval attaches to exactly one revision.
4. **Processing status ≠ publication status.** They are separate tables with separate state machines.
5. **Publication is idempotent** per (sermon, output target), so retries cannot create duplicate posts.

## 2. Components and responsibilities

| Component | Responsibility | Does NOT |
|---|---|---|
| `core` | Domain types (pydantic), module interfaces (`Protocol`s), errors, config schema | Import any module implementation |
| `db` | SQLAlchemy 2.0 models, Alembic migrations, repositories | Contain business rules |
| `storage` | `ArtifactStore`: put/get/open-local-path; `LocalStore`, later `S3Store` | Know what artifacts mean |
| `jobs` | SQLite-backed job queue, worker loop, leases, retries/backoff, periodic scheduler | Know stage semantics |
| `pipeline` | Stage graph, cache keys, running a stage = load inputs → call module → store outputs; creating machine revisions | Talk HTTP |
| `review` | Revisions, diffs, approvals, "what is stale" logic | Call external services |
| `publish` | Publication records, idempotency, invoking outputs, retry | Decide *what* is approved |
| `media` | FFmpeg/ffprobe wrapper (probe, extract, cut, fade, loudnorm, encode, tag) | — |
| `llm` | Thin provider-neutral chat/completion client used by formatter + metadata modules | — |
| `modules/inputs/*` | `google_drive` (first), `upload`, `local_dir`; later `youtube`, `s3` | — |
| `modules/processors/*` | `classify` (audio state segmentation), `detect` (sermon boundaries), `transcribe`, `format`, `metadata` | — |
| `modules/outputs/*` | `wordpress` (first), later `s3_podcast`, `filesystem` | — |
| `web` | FastAPI app: server-rendered Jinja2 + htmx pages, JSON API, auth, audio streaming | Run long work inline (enqueues jobs) |
| `cli` | `georg serve`, `georg worker`, `georg ingest <file/url>`, `georg run-stage`, `georg user add`, `georg db upgrade` | — |

Why a monolith with two process roles: one image, one deploy, one DB; the worker can be killed/restarted without affecting the UI. For a single small deployment, `georg serve --with-worker` can run both in one process.

## 3. Module interfaces (contracts)

The authoritative definitions are `georg.core.types` and `georg.core.contracts`. The sketch below is the original abridged design; the code adds four-point `ServiceBoundaries`, multi-part `SourceItem`s, transcript-aware boundary detection, and outputs that declare which renditions they need.

```python
# ---- shared value types ----
class TimeRange(BaseModel):          # milliseconds, end exclusive
    start_ms: int
    end_ms: int

class AudioSegment(BaseModel):       # output of classification
    range: TimeRange
    label: Literal["silence", "noise", "music", "speech"]
    confidence: float | None = None

class Transcript(BaseModel):         # provider-neutral
    language: str
    segments: list[TranscriptSegment]   # text + TimeRange (+ optional words w/ timestamps)

class SermonMetadata(BaseModel):
    title: str | None
    preacher: str | None
    service_date: date | None
    scripture: list[str]             # free-form references, e.g. "John 3:1-17"
    series: str | None
    occasion: str | None             # e.g. liturgical day; optional, not Lutheran-specific
    summary: str | None
    extra: dict[str, Any]            # passthrough for output-specific fields

# ---- inputs ----
class SourceItem(BaseModel):
    source_type: str                 # "youtube", "upload", ...
    source_ref: str                  # stable unique id within source_type (dedupe key)
    title: str | None
    recorded_at: datetime | None
    url: str | None
    hints: dict[str, Any]            # e.g. YouTube description, for metadata generation

class InputSource(Protocol):
    def discover(self, since: datetime | None) -> Iterable[SourceItem]: ...   # completed recordings only
    def fetch(self, item: SourceItem, dest_dir: Path) -> Path: ...           # returns local media file

# ---- processors ----
class AudioClassifier(Protocol):
    version: str
    def classify(self, audio: Path) -> list[AudioSegment]: ...

class SermonDetector(Protocol):
    version: str
    def detect(self, segments: list[AudioSegment], duration_ms: int,
               params: DetectorParams) -> BoundaryProposal: ...   # range + confidence + explanation

class Transcriber(Protocol):
    version: str
    def transcribe(self, audio: Path, language: str | None) -> Transcript: ...

class TranscriptFormatter(Protocol):
    version: str
    def format(self, transcript: Transcript, ctx: FormatContext) -> FormattedTranscript: ...  # Markdown

class MetadataGenerator(Protocol):
    version: str
    def generate(self, ctx: MetadataContext) -> SermonMetadata: ...  # ctx: source hints, transcript, config defaults

# ---- outputs ----
class PublicationRequest(BaseModel):
    publication_key: str             # stable idempotency key: f"georg:{instance}:{sermon_id}"
    metadata: SermonMetadata
    transcript_markdown: str | None
    audio: ArtifactHandle | None     # local path + optional public URL
    existing: ExternalRef | None     # set when updating a previous publication

class OutputTarget(Protocol):
    def publish(self, req: PublicationRequest) -> ExternalRef: ...           # create or update
    def find_existing(self, publication_key: str) -> ExternalRef | None: ... # duplicate guard
```

Module selection is by configuration (`georg.toml`): each configured instance names a module type and options. A plain registry dict maps type names to classes; Python entry points can be added later for third-party modules without changing the contracts.

## 4. Core data model (SQLite)

```
users            id, username, password_hash (argon2), role [admin|reviewer], created_at

services         id, source_type, source_ref  UNIQUE(source_type, source_ref),
                 title, service_date, duration_ms, discovered_at, status_summary

artifacts        id, service_id, kind, storage_key, sha256, size, mime,
                 produced_by_run_id, meta_json, created_at
                 kinds: source_media, service_audio, analysis_audio, segments,
                        boundary_proposal, sermon_audio, transcript_raw,
                        transcript_formatted, metadata_proposal

stage_runs       id, service_id, stage, module, module_version, params_json,
                 input_artifact_ids, cache_key, status [queued|running|succeeded|failed|cancelled],
                 attempt, error, started_at, finished_at

jobs             id, kind, payload_json, status, run_after, lease_until, worker_id,
                 attempts, max_attempts, last_error,
                 failure_notified_at                     -- the queue

sermons          id, service_id, current_revision_id     -- one per service now; schema allows more

sermon_revisions id, sermon_id, parent_revision_id, number, author [system|user:<id>],
                 range_start_ms, range_end_ms, metadata_json, transcript_markdown,
                 sermon_audio_artifact_id, field_origins_json, content_hash, created_at
                 -- immutable; every edit = new row

approvals        id, revision_id, approved_by, approved_at, note
                 -- approved  ⇔  an approval exists for sermons.current_revision_id

publications     id, sermon_id, target_name, revision_id, publication_key,
                 status [pending|publishing|published|failed|superseded],
                 external_id, external_url, attempts, last_error, updated_at
                 UNIQUE(sermon_id, target_name)  -- one external object per target
```

Notes:
- `field_origins_json` records per field whether the value is `machine` or `edited`. Regenerating a field never overwrites an `edited` value without an explicit user action.
- `content_hash` covers everything that would be published (range, metadata, transcript, audio sha256). Approval applies to the revision; since edits create a new revision, a post-approval edit is automatically unapproved.
- Configuration (inputs, outputs, detector parameters, `require_approval`) lives in a TOML file plus environment variables for secrets — not in the DB. Effective parameters are stored with each `stage_run` for reproducibility.

## 5. Workflows

### 5.1 Processing pipeline

| # | Stage | Input → Output | Cost | Re-run when |
|---|---|---|---|---|
| 1 | acquire | SourceItem → `source_media` | download | rarely |
| 2 | prepare_audio | source → `service_audio` (FLAC, full quality) + `analysis_audio` (16 kHz mono) | cheap | rarely |
| 3 | classify | analysis audio → `segments` (silence/noise/music/speech) | **expensive (ML)** | classifier version changes |
| 4 | detect | segments + params → `boundary_proposal` | trivial | parameters change |
| 5 | transcribe | service audio → `transcript_raw` (timestamped) | **expensive ($ or CPU)** | provider changes |
| 6 | extract_sermon | service audio + revision range → `sermon_audio` (MP3, padding, fades, loudness-normalized, ID3 tags) | cheap | range changes |
| 7 | format | transcript slice for range → `transcript_formatted` | local (`llama_cpp`, default) or hosted (`openai_chat`) LLM call | range changes (if not hand-edited) |
| 8 | metadata | source hints + formatted transcript + config defaults → `metadata_proposal` | LLM call | on request |
| 9 | draft | assemble revision #1 (author=system), mark "ready for review", notify | — | — |

Proposed choice (decision D3): **transcribe the whole service once**, then slice by timestamps for the sermon. Moving a boundary in review then only re-cuts audio and re-slices/re-formats text — no re-transcription. API cost difference is small (≈90 vs ≈25 minutes of audio per week), and a full-service transcript enables a future text-based detection strategy.

Execution: the worker claims jobs from the `jobs` table (`UPDATE … RETURNING` with a lease), runs one job at a time by default, heartbeats the lease, and on failure retries with exponential backoff up to `max_attempts`, then marks the stage `failed` (visible in UI, with "Retry") and emails the `review.notify` reviewers once per failed job (see [Email notices](deploy-aws.md#email-ses)). Stale leases (crashed worker) are re-queued. Any single stage can be re-run from the UI/CLI; downstream stages whose inputs changed are marked stale and re-queued.

### 5.2 Review

1. Reviewer opens the service: waveform (wavesurfer.js) with color-coded classification segments, proposed sermon region, and draggable start/end handles; can play the sermon or ±N seconds around each boundary.
2. Edits to range, metadata, or transcript create a new revision. A range change enqueues `extract_sermon` + `format` (format only if the transcript hasn't been hand-edited; otherwise the UI offers "regenerate from new range").
3. "Approve" records an approval for the current revision (only allowed once all derived artifacts for that revision are ready).
4. Revision history with diffs is visible; any revision can be restored (which creates a new revision).

### 5.3 Publication

1. On approval (or immediately after draft if `require_approval = false` for a target), a `publications` row is created/updated per configured output target → job enqueued.
2. Publish job: if `external_id` is known → update that object. Otherwise call `find_existing(publication_key)` (the key is stored in the external system, e.g. WordPress post meta) → update if found, else create. Record `external_id` immediately after creation.
3. Approving a later revision of an already-published sermon re-publishes as an **update** of the same external object, never a new post.
4. Failures are retried with backoff and surfaced with "Retry publish" in the UI.

Status visible to the reviewer per service: *processing* (per-stage), *review* (needs review / approved rev N / edited since approval), *publication* (per target).

### 5.4 WordPress / ChurchThemes specifics

Verified against the Church Content 2.7 plugin source and the live site (WordPress 7.1.4):

- Sermons are post type `ctc_sermon`. Taxonomies `ctc_sermon_series`, `ctc_sermon_speaker`, `ctc_sermon_book`, `ctc_sermon_topic`, `ctc_sermon_tag` are all REST-enabled, so posts and terms can be created with the normal REST API and an application password.
- The audio link is protected post meta `_ctc_sermon_audio`, which REST cannot write by default (the plugin never calls `register_post_meta`).
- **How St. Paul currently uses the fields** (from `new_sermon.pl`):

  | Georg field | WordPress | Example |
  |---|---|---|
  | title | `post_title` | "Great Expectations" |
  | preacher | `ctc_sermon_speaker` term | "Rev. Richard Mittwede" |
  | scripture | `ctc_sermon_book` term (stores the full reference) | "Luke 12:35-40" |
  | occasion (liturgical day) | `ctc_sermon_series` term | "New Year's Eve" |
  | date + hour | `post_date` / `post_date_gmt` (America/Chicago) | 2018-12-31 18:00 |
  | transcript | `post_content`, prefixed "[Machine transcription]" | — |
  | audio | file `wp-content/uploads/sermons/stpaul_sermon_<date>_<slug>.mp3`, a media-library attachment whose `_wp_attached_file` = `sermons/<file>`, and `_ctc_sermon_audio` = its public URL | — |

  These mappings are per-site configuration of the WordPress output, not hard-coded, because other ChurchThemes sites use series and book differently.
- **Integration mechanism.** The current scripts log in to wp-admin and scrape forms, plus write directly to MySQL. Georg will run off-server, so it uses a tiny companion plugin `georg-bridge` (AGPL, kept in this repo) whose REST routes accept only HMAC-SHA256-signed requests (a per-site key in `wp-config.php`; Wordfence disables application passwords on the live site), acting as a WordPress user configured on the site (the existing `scripts` user). Protocol, setup and rotation: `docs/wordpress.md`. The plugin:
  1. `POST /georg/v1/audio` uploads sermon audio into `uploads/sermons/` as a proper attachment, filling `_wp_attachment_metadata` (mime type, size, duration). This keeps `podcast.pl` and the hourly R2 sync working unchanged.
  2. `GET`/`PUT /georg/v1/sermons/<publication_key>` find, create, or update the `ctc_sermon` post carrying `_georg_publication_key`, set its terms, and write `_ctc_sermon_audio` server-side (instead of exposing the protected meta to the generic REST API). The configured user needs `edit_others_posts` and `upload_files` (filterable).
  If installing a plugin is unwelcome, the same PHP can go in the already-installed Code Snippets plugin.

### 5.5 Podcast feed and audio hosting

- Audio URLs `https://www.stpaulaustin.org/wp-content/uploads/sermons/…` are 302-redirected to `media.stpaulaustin.org` (Cloudflare R2). An hourly cron job, `rclone copy`, pushes new files from `uploads/sermons/` to R2.
- `podcast.pl` builds the RSS feed straight from the WordPress database: sermon posts, series and speaker terms, `_ctc_sermon_audio`, and the attachment's `_wp_attachment_metadata`. It **stops with an error** if the audio URL doesn't match `/wp-content/uploads/sermons/*.mp3` or the attachment record is missing. That is why the bridge must create real attachments.
- Recommendation (D12): **keep `podcast.pl` and the R2 sync as they are for the MVP.** Georg publishes into WordPress exactly as the current upload form does. A generic podcast-feed output module can come later for churches without such a setup.

### 5.6 Findings from the existing scripts

- **Transcription** (`transcribe_sermons.pl`): lemonfox.ai's Whisper-compatible `/v1/audio/transcriptions`, given the public audio URL, `language=English`, `response_format=verbose_json` (segment timestamps). The raw JSON is saved in post meta `stpaul_sermon_transcription_raw`. It retries when lemonfox times out fetching the URL. Cost is low, and because the API is OpenAI-compatible, one `Transcriber` implementation with a configurable base URL covers lemonfox, OpenAI, and self-hosted Whisper servers.
- **Formatting** (`format_sermons.pl`): OpenAI `gpt-4o-mini` with a strict "do not change, add, or remove any words; only add paragraph breaks and punctuation" prompt. Georg keeps this rule and adds an automated check: compare the formatted text's word sequence with the raw transcript's and flag any difference in review. Long sermons may need chunking to stay within output limits.
- **Metadata** today is typed by hand on the upload form (title, preacher, scripture, liturgical day, date, hour). Georg proposes values from the Drive filename and folder, from configured defaults (usual preacher, service time), and optionally from an LLM reading the transcript (e.g. scripture references). The reviewer confirms them.
- **Secrets:** Georg reads secrets only from the environment or a secret store (SSM on AWS) and never from configuration files in the repository.
- **Hosting:** the church web server is too small to run Georg itself, which confirms the separate-host plan.

### 5.7 Findings from the church processing machine, the timing POC, and `stpaul-audio`

**Current automated flow** (church machine, cron): an rclone mount of the Drive at `/mnt/gdrive` → `fetch_google_drive_recordings.sh` → `find_service_timing.pl` → `process_service.pl`.

- **Drive layout.** One folder per Sunday named `SPX MM.DD.YY`. It holds the service video(s) (`.mp4/.mkv/.mov/.m4v`) and a Word document named like `2026-11-01_All_Saints__Day_-_DS1-Eric.docx` (date, liturgical day, service setting, preacher). The document arrives days before the video, so it is a useful **metadata hint** (occasion, preacher) and the metadata generator should parse its filename (and optionally its text).
- **Extraction.** ffmpeg, video → MP3 192 kbps / 44.1 kHz. The fetcher waits until file size is stable for 60 s, takes the *first* video in the folder, and has a hard-coded cutoff date. A folder can contain more than one service; today a person picks the right one ("prefer the intended service; if important microphones were off, use the other").
- **Four boundaries, not two.** Every service gets four timestamps measured on the original extracted audio: *service start, sermon start, sermon end, service end*, with `0 ≤ service_start ≤ sermon_start < sermon_end ≤ service_end ≤ duration`. Georg's detector proposes all four and the review UI edits all four.
- **Three renditions per service** (`process_service.pl`): trim to the service → dynamic-range compression (Mono-hosted .NET "Chris's Dynamic Compressor", `-f:-80 -r:.1 --attack-time:3 --release-time:2`) → ReplayGain normalisation (`wavegain -n -s -y`) → LAME:
  - **HQ service audio**, VBR `-V 2` stereo — intended for a Drive archive, but the upload code is commented out;
  - **telephone service audio**, `-b 32 -a -h`, uploaded by `scp` to the web server's `/var/www/html/phone/services/YYYY-MM-DD.mp3` (a dial-in service-listening feature);
  - **sermon audio**, mono VBR `-V 2`, then uploaded by hand through `new_sermon/`.
- **Timing algorithm** (`find_timing.pl`, both copies essentially identical):
  1. Recordings longer than 2.5 h: mean volume per 10-minute window (`volumedetect`, < −50 dB = silent) to find the last service.
  2. Service start: classify a window around it with **inaSpeechSegmenter** (labels `male/female/music/noise/noEnergy`); the earlier of the first sustained speech (> 5 s) or sustained music (> 90 s).
  3. Service end: the last music segment in the closing window, plus a 10 s tail.
  4. Sermon: probe 6-second chunks at 5/12, 6/12, 4/12 of the service until one is "speech only" (no music, more speech than noise); walk outwards in 2-minute steps until a probe fails. Sermon start = first speech segment in the 5-minute window before that (+0.1 s). Sermon end = end of the last transcription segment with `exp(avg_logprob) > 0.9` in the 5-minute window after it (a lemonfox call).
  - The MFCC+SVM noise-vs-music classifier (`noise_vs_music.py` + two `.joblib` models) is computed but its output is not used.
- **Earlier rewrite effort** (`XanC/stpaul-audio`, October 2026): inventories all of the above, proposes a per-service job record with the four timings and per-stage status, and proved an isolated on-demand EC2 encoding worker and a read-only Drive-intake worker (us-east-2). Its rewrite plan lists real defects (racy lock files, completion marker that checks a file never written, arbitrary first-video selection, shared temp paths, unverified uploads, duplicate-prone WordPress scraping). Georg supersedes it and adopts its validations: timestamp-order checks, output-duration checks, full decode of each output before upload, and a fresh working directory per run.
- An existing ffmpeg recipe for clip video (`service_encode.sh`: x264 CRF 26, AAC 128k, `highpass+acompressor+dynaudnorm`) is a starting point for the later sermon-video output.

**Consequences for Georg**

- *Domain:* a `Service` has one selected source recording, four boundaries (proposed → reviewed), and N **renditions** produced by a configurable mastering chain. Outputs consume renditions: WordPress gets the sermon rendition; a **file-drop output** (SFTP/local directory) delivers the telephone rendition; an optional Drive output archives HQ audio.
- *Classifier:* `InaSpeechSegmenterClassifier` (MIT; TensorFlow) run **once over the whole service**, cached as an artifact; the detector is then a pure function over segments plus the transcript. Because Georg transcribes the service anyway, sermon-end refinement reads the cached transcript instead of making extra API calls.
- *Mastering:* default to an **ffmpeg-native chain** (`acompressor` + loudness normalisation) so the image needs no Mono, 32-bit libc, or unlicensed binaries. Phase 1 acceptance includes an A/B listening comparison against the legacy chain on two past services; the legacy chain can remain an optional, unbundled processor if the ffmpeg chain is not good enough.
- *Drive input:* discover `SPX MM.DD.YY` folders; a video is "complete" when its Drive `md5Checksum`/size are unchanged across two polls; dedupe on Drive file ID; when a folder has several videos, apply a configurable preference and let the reviewer switch recordings (re-running detection).
- *Transition:* the church machine keeps running until Georg's outputs are verified; then its cron job is disabled.

## 6. Repository structure

```
~/georg/repo/  (→ github.com/<owner>/georg)
├── LICENSE                     # full, unmodified AGPL-3.0 text
├── README.md                   # purpose, Georg Rörer, install, AI-authorship/copyright note
├── CONTRIBUTING.md             # AGPL contribution terms, workflow, no CLA
├── AGENTS.md                   # conventions + rules for coding agents
├── pyproject.toml              # uv, ruff, mypy, pytest config
├── src/georg/
│   ├── core/  db/  storage/  jobs/  pipeline/  review/  publish/  media/  llm/
│   ├── modules/
│   │   ├── inputs/      google_drive.py  upload.py  local_dir.py
│   │   ├── processors/  classify/  detect/  transcribe/  format/  metadata/
│   │   └── outputs/     wordpress/  (+ wordpress-plugin/ companion PHP plugin, see §5.4)
│   ├── web/             app.py  routes/  templates/  static/
│   └── cli.py
├── tests/  unit/  integration/  fixtures/   # synthetic audio generated by ffmpeg in tests
├── eval/                       # sermon-detection evaluation harness + golden-set manifest (no audio in git)
├── docs/  architecture.md  modules.md  deployment-aws.md  adr/0001-…md
├── deploy/  Dockerfile  compose.yaml  Caddyfile  aws/ (cloud-init, IAM policy JSON)
└── .github/  workflows/ci.yml  ISSUE_TEMPLATE/task.md  pull_request_template.md
```

Tooling: Python 3.12, `uv`, `ruff` (lint+format), `mypy --strict` on `core`/`pipeline`, `pytest`. CI runs lint, types, tests, an SPDX-header check (`# SPDX-License-Identifier: AGPL-3.0-only`, no copyright line), and a dependency-license allow-list check (`pip-licenses`). Dockerfile build is checked in CI.

Web UI stack: server-rendered Jinja2 + htmx + wavesurfer.js (regions plugin). No SPA build chain. Footer shows version and a "Source code" link (AGPL §13 network-use offer; URL configurable for forks).

## 7. AWS deployment (initial): stop-when-idle single instance

Georg typically handles one service per week, so the instance is **stopped most of the time**. The application is unchanged; only deployment adds three small pieces.

Runbook and implementation: [`docs/deploy-aws.md`](deploy-aws.md), `deploy/aws/`.

- **One EC2 instance** (start t3.medium / t4g.medium; size after measuring the classifier) running Docker Compose: `georg-web`, `georg-worker`, `caddy` (automatic HTTPS; certificates persist on EBS).
- **EBS gp3 volume** (~50 GB) for SQLite (WAL mode) and the local artifact store. It persists while the instance is stopped. SQLite must *not* live on EFS/NFS, which is why Fargate/App Runner/Lambda are not recommended.
- **Wake on schedule:** EventBridge Scheduler starts the instance after the usual service time. The worker polls the input (Google Drive) until the recording appears or a timeout passes, processes it, and emails "ready for review" (SES).
- **Wake on demand:** the review link points to a small Lambda function URL. It starts the instance, shows a "starting, about 1 minute" page, then redirects to the admin UI.
- **Sleep:** an idle watchdog in Georg (`idle_shutdown_minutes`, **off by default**) powers the instance off when no jobs are queued/running and there have been no web requests for N minutes.
- **DNS:** on boot the instance updates its Route 53 record (no Elastic IP billed while stopped).
- **Backups:** Litestream replicates SQLite to S3 while running; artifacts sync to S3 nightly/after jobs; EBS snapshots via Data Lifecycle Manager.
- Secrets in SSM Parameter Store; S3/SES/Route 53 access via instance IAM role.
- **Cost ballpark** (list prices, unverified): about $2/month compute at ~10 h/week + ~$4 EBS + small S3/Route 53/Lambda ≈ **$7–10/month**, plus transcription/LLM API usage. Always-on would be ~$12–30/month.
- **Portability:** with the watchdog off, the same compose file runs always-on on any VPS or office machine. The wake Lambda, schedule, and DNS hook live in `deploy/aws/` and are optional.

**Input note:** original recordings come from a **private Google Drive** folder, which avoids YouTube's datacenter-IP blocking. Access: either a Google Cloud service account shared on the folder, or (for a congregation without Google Workspace, as at St. Paul) a congregation-owned Google account that signs in once with `georg drive-login`; its storage also holds the Drive archive, which a service account cannot own outside a shared drive. YouTube remains a possible later input.

## 8. Licensing implementation

- `LICENSE`: standard AGPL-3.0 text, unmodified. `pyproject.toml`: `license = "AGPL-3.0-only"`.
- Source files: SPDX line only; no generated copyright notices.
- README section "Copyright and AI-generated code": explains that much of the code is AI-generated, may not be copyrightable under current U.S. law, and that the AGPL applies to copyrightable material to the extent rights exist — without altering the license.
- CONTRIBUTING: contributions are submitted under AGPL-3.0-only to the extent the contributor has rights; no copyright assignment; DCO not required (can add later).
- `docs/dependencies.md` + CI allow-list: MIT, BSD, Apache-2.0, ISC, PSF, LGPL (as library), MPL-2.0, GPL-3.0/AGPL-3.0 compatible. Notable: yt-dlp (Unlicense), FFmpeg invoked as a subprocess (container ships the distro build; its GPL/LGPL source is available via the distro — documented), TensorFlow (Apache-2.0) if the classifier needs it.
- Reference POC scripts are kept out of the repo by default (see Q8).

## 9. Unresolved design decisions

| ID | Decision | Recommendation | Needs you? |
|---|---|---|---|
| D1 | Web UI: server-rendered htmx vs SPA | htmx + Jinja2 | No |
| D2 | Job queue: SQLite table vs Redis/Celery | SQLite table | No |
| D3 | Transcribe full service vs sermon only | Full service, slice by timestamps | Light (cost) |
| D4 | Transcription provider | Local Whisper via faster-whisper (CPU, int8) by default, to drop the lemonfox subscription; OpenAI-compatible `Transcriber` (lemonfox.ai, OpenAI, self-hosted servers) kept as an option | No |
| D5 | Audio classifier dependency | inaSpeechSegmenter (confirmed from POC), whole service once, as an optional extra; drop the unused SVM | No |
| D6 | Media hosting for published audio | Unchanged: `uploads/sermons/` on the web server, synced to R2 by the existing cron | No |
| D7 | WordPress integration mechanism | `georg-bridge` plugin REST routes (meta + attachment endpoint) with HMAC-signed requests, §5.4 | Confirm plugin OK |
| D8 | Source-media retention | Keep audio forever; keep source video N days (configurable) — video features later can re-fetch | Light |
| D9 | Auth | Local users, argon2, session cookies; 2 roles | Light (Q5) |
| D10 | Artifact store on AWS | Local EBS + S3 backup now; S3Store in a later phase | No |
| D11 | Multiple sermons per service | Answered: one sermon per service; schema still allows N, detector proposes 1 | No |
| D12 | Podcast feed: keep `podcast.pl` vs Georg output module | Keep `podcast.pl` + R2 sync; Georg writes data compatible with them | No |
| D13 | Google Drive auth: service account vs user OAuth | **Decided:** both supported; St. Paul (no Workspace) signs in as its own church Google account, which also stores the archive | No |
| D14 | Hosting: stop-when-idle EC2 vs always-on | Stop-when-idle (owner agreed in principle) | No |
| D15 | Mastering chain | **Decided:** ffmpeg-native (`acompressor` + loudness normalisation), A/B-checked by ear against the legacy Mono compressor + wavegain | No |
| D16 | Renditions in the MVP | **Decided:** sermon → WordPress; telephone service audio → SFTP output; HQ service audio → Google Drive output | No |
| D17 | Several videos in one Drive folder | **Decided:** usually one recording that was interrupted and restarted, so treat the videos as ordered parts of one source and concatenate them; reviewer can exclude a part | No |
| D18 | AWS account | New account intended for transfer to the church vs the existing us-east-2 project used by `stpaul-audio` | **Yes** |

## 10. Phased plan to a minimal end-to-end system

**Phase 0 — Foundation (architect + 1 agent).** Repo, LICENSE/README/CONTRIBUTING/AGENTS.md, CI, ADRs 0001–0005, `core.contracts` and domain types, config schema, issue/PR templates, GitHub branch protection.
*Exit:* CI green on `main`; contracts merged and reviewed.

**Phase 1 — Headless pipeline (parallel agents).** `media`, `classify` (POC port), `detect` (POC port, configurable), `transcribe`, `format`, `metadata` (Perl prompts ported), DB + local store + job queue + stage caching. CLI: `georg ingest <file|drive-file-id>` runs the full pipeline. Google Drive input. Detection eval harness against a golden set of 10–20 past services with known boundaries (past Drive recordings + published sermon audio).
*Exit:* one command turns a real recording into sermon MP3 + formatted transcript + metadata; detection accuracy measured.

**Phase 2 — Review UI.** Auth, service list, waveform/boundary editor, metadata + transcript editing, revisions, approval, retry buttons.
*Exit:* a reviewer can correct and approve a real service end to end in a browser.

**Phase 3 — WordPress output → MVP.** WordPress output module with idempotent create/update, publication status, retry. Manual URL/upload input is enough.
*Exit (MVP):* recording → automatic processing → human review → published sermon on the church site, with no duplicate posts under retry.

**Phase 4 — Automation & deployment.** Drive discovery polling, email notification "ready for review", stop-when-idle AWS deployment (compose, Caddy, Litestream, DLM, wake Lambda, schedule, idle watchdog), runbook.
*Exit:* runs unattended on AWS for several consecutive Sundays.

**Later.** S3 artifact store, unattended publishing, full-service audio output, video extraction, additional providers/integrations, multi-sermon detection.

## 11. First tasks for implementation agents

Each becomes a GitHub issue with: context, interface it must satisfy, acceptance criteria, out-of-scope list. Agents work on `agent/<issue#>-<slug>` branches; the integration reviewer reviews; the architect (or you) merges.

| # | Task | Depends on | Key acceptance criteria |
|---|---|---|---|
| T0 | Repo scaffold + CI (uv, ruff, mypy, pytest, SPDX check, license check, Dockerfile build) | — | CI green; `uv run pytest` works from clean clone; Docker image builds with ffmpeg |
| T1 | `core.contracts` + domain types + config schema | T0 | **Architect writes this personally**; reviewed before others start |
| T2 | `media` FFmpeg wrapper | T1 | probe/extract/cut/fade/loudnorm/encode/tag; tests use ffmpeg-generated audio; no network |
| T3 | Boundary detector port (pure function over segments + transcript) | T1, POC | proposes all four boundaries; parameters from config; unit tests on synthetic segment lists incl. edge cases (long multi-service recording, no speech-only probe, very short/long sermons); explanation string |
| T4 | Audio classifier (inaSpeechSegmenter) | T1, POC | implements `AudioClassifier`; segments JSON round-trips; heavy deps behind optional extra; runtime/memory measured on a 2-hour file |
| T5 | DB schema + migrations + `LocalStore` + job queue | T1 | Alembic migration from empty; lease/retry/stale-lease tests; content-addressed artifacts |
| T6 | Transcriber (OpenAI-compatible; lemonfox default) | T1 | chunking for API size limits; timestamps preserved across chunks; recorded-response tests, no live API in CI |
| T7 | Formatter + metadata generator (port Perl prompt) | T1 | prompts in versioned template files; word-sequence preservation check; deterministic tests with mocked LLM |
| T8 | `georg-bridge` WordPress plugin + WordPress output module | T1 | idempotent create/update by `_georg_publication_key`; attachment in `uploads/sermons/` with `_wp_attachment_metadata`; `podcast.pl` output unchanged on a staging copy |

| T9 | Mastering chain + renditions | T2 | ffmpeg-native compressor/normaliser; sermon, telephone and HQ presets matching today's LAME settings; duration and full-decode checks; A/B samples for listening |
| T10 | File-drop output (SFTP/local) | T1 | idempotent upload of the telephone rendition to `phone/services/YYYY-MM-DD.mp3`; atomic rename; verified size |

T2–T7 and T10 can run in parallel once T1 is merged. Phase 1 integration (`pipeline` orchestrator + CLI) is assigned after T5 lands.

## 12. Questions for you

Answered: Georg replaces `stpaul-audio` (to be deleted) and the church machine's processing (D15–D17 decided); ChurchThemes (Church Content); audio on the church site plus a bespoke feed (kept as is); Drive is the source of recordings, accessed with a service account; recordings arrive Sunday afternoons; no on-site machine; one sermon per service; `georg-bridge` plugin approved; initial reviewer is the owner (recipients configurable, multiple); repo `XanC/georg`; POCs may be published once redacted; plan approved.

Open:
1. D18: new AWS account, or the existing us-east-2 project? Subdomain for the admin UI?
2. Is there a staging copy of the website for testing publication?
