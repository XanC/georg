# Google Drive

Georg reads service recordings from a Google Drive folder (`google_drive` input) and can archive the HQ full-service MP3 to Drive (`google_drive_upload` output). Both authenticate as a Google Cloud **service account**, so no personal OAuth token has to be refreshed. Install the optional dependency with `uv sync --extra gdrive`.

## Setting up the service account

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

The input requests the read-only scope `drive.readonly`; the output requests `drive`, but can only reach files and folders shared with the service account.

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

`credentials_file` defaults to `env:GOOGLE_APPLICATION_CREDENTIALS`; `folder_id` may be a literal ID or an `env:` reference.

Input options: `folder_pattern` (default `"SPX *"`), `date_format` (default `"%m.%d.%y"`, parsed after the pattern's literal prefix), `video_extensions` (default mp4, mkv, mov, m4v), `stable_minutes` (default 10: a folder is reported only once every video has a size and was last modified at least this long ago), `include_shared_drives` (default true). Every video in a folder is a part of one recording, ordered by name and then creation time; other files (e.g. the `.docx` service sheet) become `hints["documents"]`. Downloads are verified against Drive's size and MD5, and already downloaded parts are reused.

Output options: `filename` (default `"{date}.mp3"`), `subfolder` (optional, created if missing), `rendition` (default `service_hq`). Templates accept `{date}` (YYYY-MM-DD), `{mmddyy}` (MM.DD.YY), `{yyyy}`, `{yy}`, `{mm}`, `{dd}`. Each file carries the publication key in `appProperties.georg_publication_key`, so a retried or repeated publication updates the same file instead of creating a duplicate.
