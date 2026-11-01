# Testing the WordPress output locally

There is no staging copy of the church site, so `georg-bridge` and the `wordpress` output
are checked against a throwaway WordPress in Docker before a plugin change goes live.
Everything lives in `scripts/wordpress-e2e/`:

- `compose.yaml`: WordPress (PHP 8.3, Apache) + MariaDB, `WP_DEBUG`/`WP_DEBUG_LOG` on,
  `WP_ENVIRONMENT_TYPE=local` (Application Passwords then work over plain HTTP), the
  repository's `wordpress/georg-bridge` bind-mounted read-only, and small PHP upload limits
  (`php/uploads.ini`: 8M / 12M) so the oversize errors can be tested.
- `mu-plugins/georg-e2e-theme-support.php`: declares Church Content sermon support the way
  a ChurchThemes theme does (ChurchThemes themes are paid; the default theme is used).
- `setup.sh`: starts the stack, installs WordPress, installs and activates Church Content
  (`church-theme-content` from wordpress.org) and Georg Bridge, and creates an Editor
  (`georg`), an Author and a Subscriber with Application Passwords, written to the
  git-ignored `scripts/wordpress-e2e/.env.e2e`.
- `e2e.py`: publishes through `georg.modules.outputs.wordpress.WordPressOutput` with
  ffmpeg-generated MP3s and inspects the result with wp-cli.

## Run

Needs Docker with Compose v2, ffmpeg, and `uv sync`.

```sh
scripts/wordpress-e2e/setup.sh            # GEORG_WP_PORT=8080 by default
uv run python scripts/wordpress-e2e/e2e.py
```

It prints one PASS/FAIL line per check and exits non-zero on any failure. Each run uses
fresh publication keys, so it can be repeated against the same site. The site stays up at
http://localhost:8080 (admin / admin) for poking around; remove it with
`docker compose -f scripts/wordpress-e2e/compose.yaml down -v`.

## What it checks

- A draft `ctc_sermon` with title, `[Machine transcription]`-prefixed content, local and
  GMT dates (America/Chicago), speaker/book/series terms, and `_georg_publication_key`.
- The MP3 under `wp-content/uploads/sermons/` as an `inherit` attachment parented to the
  sermon: `_wp_attached_file = sermons/<file>`, mime `audio/mpeg`, GUID equal to the URL,
  `_wp_attachment_metadata` with `length`, `length_formatted`, `filesize`, `mime_type`,
  `dataformat`; `_ctc_sermon_audio` resolves to that attachment. This is what the church's
  `podcast.pl` reads (architecture §5.5).
- `find_existing`; republishing with the same key (same audio, then new audio) updates the
  same post and attachment without duplicates; a sermon published by hand stays published
  when Georg updates it as a draft; `status = "private"` and `"publish"`.
- 401/403 for anonymous, wrong-password, cookie-only, Author and Subscriber requests.
- Non-MP3 uploads (PHP, WAV, Ogg named `.mp3`) are rejected; path-traversal file names stay
  inside `uploads/sermons/`; oversized uploads get 413 naming the PHP limit.
- Admin and front-end pages (sermon list and edit screens, media library, previews, feeds)
  load, and `wp-content/debug.log` stays empty throughout.

Before installing a new plugin version on the live site, also confirm there that PHP's
`upload_max_filesize` and `post_max_size` exceed the largest sermon MP3 and that security
plugins (Wordfence) allow Application Passwords.
