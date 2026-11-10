# Google Drive

Georg reads service recordings from a Google Drive folder (`google_drive` input) and can archive the HQ full-service MP3 to Drive (`google_drive_upload` output). Install the optional dependency with `uv sync --extra gdrive`.

Both modules read their credentials from the JSON file named by `credentials_file`, which is one of:

- **A signed-in Google account** (`georg drive-login`). Use this when the recordings live in an ordinary Google account rather than Google Workspace: the account has its own storage, so the archive can go into a folder in its *My Drive*.
- **A service account key.** Use this with Google Workspace. A service account has no storage of its own, so the archive folder must be on a shared drive (or the service account must impersonate a Workspace user with `subject`).

Georg tells the two apart by the file's `type` field.

## Signing in as a Google account

Use an account that belongs to the congregation rather than to one person, with its password and recovery details kept by the congregation. If it changes hands, the new owner only needs the password; Georg keeps working. Switching Georg to a different account means signing in again, and archives already uploaded stay in the old account unless moved.

1. **Create a project and enable the API.** In the [Google Cloud console](https://console.cloud.google.com/), signed in as that account, create a project (e.g. `georg`) and enable the **Google Drive API** under *APIs & Services → Library*.
2. **Configure the consent screen.** Under *Google Auth Platform* (*APIs & Services → OAuth consent screen* in older consoles) choose audience **External**, give the app a name (e.g. "Georg") and the account's email as support and developer contact. Then under *Audience* choose **Publish app** so its status is **In production**. While an app is in *Testing*, Google expires its sign-ins after seven days. Verification by Google is not needed for an app only the congregation signs in to.
3. **Create the OAuth client.** Under *Clients* (*Credentials → Create credentials → OAuth client ID*), choose application type **Desktop app** and download its JSON file. It identifies the app rather than the account, but keep it out of the repository anyway.
4. **Sign in once.** On any machine with a browser and Georg installed:

   ```sh
   uv run --extra gdrive georg drive-login --client client_secret_….json --out drive-credentials.json
   ```

   Sign in as the congregation's account. Google warns that it "hasn't verified this app": choose *Advanced → Go to Georg (unsafe)*, then allow access to Google Drive. Georg prints the account it signed in as and writes `drive-credentials.json` (mode `0600`). Over SSH, add `--no-browser`: Georg prints the address to open elsewhere, and asks for the address the browser was finally sent to (a `http://127.0.0.1:…` page that does not load).
5. **Install the file on the server** as for a service account key below (`GOOGLE_APPLICATION_CREDENTIALS=/etc/georg/drive-credentials.json`). The file holds a long-lived refresh token: treat it as a password.

The sign-in requests the full `drive` scope, which the archive upload needs; `--read-only` requests `drive.readonly` for an input-only setup. The same file serves both modules.

A sign-in stops working if the account removes Georg's access (*Google Account → Security → Your connections to third-party apps*), if it goes unused for about six months, or after some account security changes. Drive jobs then fail with "the sign-in may have been revoked"; run step 4 again and replace the file.

## Setting up a service account

1. **Create a project and enable the API.** In the [Google Cloud console](https://console.cloud.google.com/), create (or pick) a project, then enable the **Google Drive API** under *APIs & Services → Library*.
2. **Create the service account.** Under *IAM & Admin → Service accounts*, create one (e.g. `georg`). It needs no project roles. Note its email, e.g. `georg@my-project.iam.gserviceaccount.com`.
3. **Create a JSON key.** On the service account's *Keys* tab choose *Add key → Create new key → JSON*. The downloaded file is a credential: never commit it or paste it anywhere.
4. **Share the folders.** In Drive, share the recordings folder (the one containing the `SPX MM.DD.YY` subfolders) with the service account's email as **Viewer**. For the archive, share the target folder as **Editor** (*Content manager* on a shared drive). Service accounts have no storage quota of their own, so the archive folder should be on a **shared drive**; uploads into a folder in someone's *My Drive* fail with `storageQuotaExceeded`.
5. **Install the key on the server.** Put it outside the repository and the data directory, readable only by the Georg user, e.g. `/etc/georg/drive-key.json` with mode `0600`, and point an environment variable at it:

   ```sh
   export GOOGLE_APPLICATION_CREDENTIALS=/etc/georg/drive-key.json
   export GEORG_DRIVE_FOLDER_ID=<id from the folder's URL: drive.google.com/drive/folders/<id>>
   export GEORG_ARCHIVE_FOLDER_ID=<archive folder id>
   ```

With a service account, the input requests the read-only scope `drive.readonly`; the output requests `drive`, but can only reach files and folders shared with the service account. A signed-in account keeps the scope it granted at sign-in.

## Configuration

```toml
[[inputs]]
name = "drive"
type = "google_drive"
options = { folder_id = "env:GEORG_DRIVE_FOLDER_ID" }

[[outputs]]
name = "archive"
type = "google_drive_upload"
renditions = ["service_hq"]
options = { folder_id = "env:GEORG_ARCHIVE_FOLDER_ID", subfolder = "SPX {mmddyy}" }
```

`credentials_file` defaults to `env:GOOGLE_APPLICATION_CREDENTIALS` and may name either kind of file; `folder_id` may be a literal ID or an `env:` reference.

Input options: `folder_pattern` (default `"SPX *"`), `date_format` (default `"%m.%d.%y"`, parsed after the pattern's literal prefix), `year_patterns` (default `["Services *"]`: service folders are also searched inside top-level folders matching these patterns, so a folder already moved into `Services 2026` is still found; set to `[]` to look at the top level only), `video_extensions` (default mp4, mkv, mov, m4v), `stable_minutes` (default 10: a folder is reported only once every video has a size and was last modified at least this long ago), `include_shared_drives` (default true). Every video in a folder is a part of one recording, ordered by name and then creation time; other files (e.g. the `.docx` service sheet) become `hints["documents"]`. Parts are not downloaded: ffmpeg streams each one straight from Drive's `alt=media` URL (which supports HTTP range reads, so MP4s whose index sits at the end of the file still work) and only the extracted audio lands in the stage cache — roughly a gigabyte per service rather than the 45–60 GB video. Each part's metadata is checked against what discovery saw, so a file replaced after discovery fails the job rather than being silently used. If a stream drops mid-read the stage fails and the job queue retries it; access tokens are refreshed automatically and never appear in logs or error messages.

Because year folders are searched, a service moved into `Services YYYY` before Georg runs is still found — so every run sees the whole archive. Set `[discovery] start_date` in the configuration (e.g. `start_date = 2026-11-01`) to the oldest service Georg should pick up automatically; anything older is ignored. `georg discover --since YYYY-MM-DD` overrides the configured date for that run to reach an older service on demand.

Output options: `filename` (default `"{date}.mp3"`), `subfolder` (optional per-service subfolder, e.g. `"SPX {mmddyy}"`), `year_patterns` (default `["Services *"]`: the subfolder is also searched inside top-level folders matching these patterns, so an archived service's folder is reused rather than duplicated at the top level), `create_in` (`"root"` (default): a missing subfolder is created under `folder_id`; `"year"`: inside the year folder named by `year_subfolder`, created if needed), `year_subfolder` (default `"Services {yyyy}"`), `rendition` (default `service_hq`). Templates accept `{date}` (YYYY-MM-DD), `{mmddyy}` (MM.DD.YY), `{yyyy}`, `{yy}`, `{mm}`, `{dd}`. Each file carries the publication key in `appProperties.georg_publication_key`, so a retried or repeated publication updates the same file instead of creating a duplicate.

Both modules share one folder-layout implementation (`georg.service_folders`) that knows nothing about Drive: another storage target reuses it by implementing `FolderStore` (list a folder's subfolders, create a folder).
