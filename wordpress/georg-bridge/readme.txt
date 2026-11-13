=== Georg Bridge ===
Contributors: georg
Tags: sermons, church, rest-api
Requires at least: 5.6
Tested up to: 7.1
Requires PHP: 7.4
Stable tag: 0.4.0
License: AGPL-3.0-only
License URI: https://www.gnu.org/licenses/agpl-3.0.html

REST endpoints that let Georg publish sermons into the Church Content plugin.

== Description ==

Georg (https://github.com/XanC/georg) turns worship-service recordings into published
sermons. This plugin is its WordPress side. It adds the REST namespace `georg/v1`:

* `POST /wp-json/georg/v1/audio` - the MP3 as the raw request body
  (`Content-Type: audio/mpeg`), with query parameters `publication_key` and optional
  `filename` and `title`. The file is stored as
  `wp-content/uploads/sermons/<sanitized filename>` (an existing file with different
  content, or one used by another attachment, is never reused; a unique name is chosen
  instead) and registered as a media-library attachment with `_wp_attachment_metadata`
  (length, length_formatted, filesize, mime_type, bitrate, ...). The file must really be
  an MP3: other audio formats named `.mp3` are rejected with 400. A body over WordPress's
  maximum upload size (the smaller of PHP's `upload_max_filesize` and `post_max_size`)
  is rejected with 413. Repeating the request with the
  same key and content returns the same attachment; new content for the same key is
  stored as a new file and the attachment is pointed at it (the old file is left in
  place, since copies elsewhere, e.g. a CDN, may still reference it).
* `GET /wp-json/georg/v1/sermons/<publication_key>` - the `ctc_sermon` post carrying this
  key in post meta `_georg_publication_key`, or 404.
* `PUT /wp-json/georg/v1/sermons/<publication_key>` - JSON body with `title`, optional
  `content`, `status` (`draft`, `private`, `publish`; default `draft`), `date`,
  `date_gmt`, `terms` (taxonomy => list of names; missing terms are created) and
  `audio_attachment_id`, and optional boolean `full_text` (true ticks Church Content's
  "Full sermon text provided" box, i.e. post meta `_ctc_sermon_has_full_text` = `1`; false
  clears it; absent leaves it alone). Creates the sermon or updates the one with this key, sets
  `_ctc_sermon_audio` to the attachment URL and attaches the audio to the post. An update
  asking for `draft` leaves a sermon that has since been published, scheduled, or made
  private in that status; `private` and `publish` are always applied.

Every request must be signed with HMAC-SHA256 using a key configured in wp-config.php
(see docs/wordpress.md in the Georg repository for the protocol). Application Passwords,
Basic auth and cookies are not accepted on these routes, and the key authenticates no
other WordPress endpoint. Signed requests act as the user named in `GEORG_BRIDGE_USER`,
who must be able to edit others' posts and, for audio, upload files (`publish` also needs
the sermon type's publish capability). The `georg_bridge_permission` filter can change
this check. HTTPS is required.

The plugin requires Church Content (post type `ctc_sermon`).

== Installation ==

1. Copy the `georg-bridge` directory to `wp-content/plugins/` and activate it.
2. Create or pick a user with the Editor role (or another role with `edit_others_posts`
   and `upload_files`).
3. Generate a key:
   `python -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode())"`
4. Add to wp-config.php:
   `define( 'GEORG_BRIDGE_KEYS', array( '2026-11' => '<key>' ) );` and
   `define( 'GEORG_BRIDGE_USER', '<login>' );`
5. Give Georg the site URL, the key id (`key_id = "2026-11"`) and the key (as an
   environment variable referenced by `key = "env:GEORG_WP_KEY"`).

To rotate, add a second id to `GEORG_BRIDGE_KEYS`, switch Georg to it, then remove the
old id. The Georg host and the server need synchronized clocks (requests older or newer
than five minutes are refused). PHP's `upload_max_filesize` and `post_max_size` must
exceed the largest sermon MP3.

== Changelog ==

= 0.4.0 =
* After every sermon write, store `_georg_written`: a sha256 per field (title, content,
  date, terms:<taxonomy>) of the values as saved, and `written_at`.
* Optional `fields` and `overwrite` on the sermon PUT. A field edited on the website since
  Georg wrote it is only written when listed in `overwrite`; otherwise the PUT returns 409
  `georg_edited_on_site` with `fields`, `modified_gmt` and `modified_by` and writes nothing.
  Sermons written before 0.4.0 count as edited when someone other than the bridge user
  saved a revision after the bridge user's last one.
* Signed GET `/georg/v1/sermons/<key>/status`: post ID, link, fields edited on the website,
  last modified time and author, and the bridge version.

= 0.3.0 =
* Optional `full_text` on the sermon PUT sets or clears `_ctc_sermon_has_full_text`
  (Church Content's "Full sermon text provided" checkbox).

= 0.2.1 =
* Delete the temporary upload with unlink() so media plugins hooking wp_delete_file (e.g. WebP Express) do not log errors.

= 0.2.0 =
* Authenticate georg/v1 requests only by HMAC-SHA256 signature (key id, timestamp,
  nonce, method, route, query and body hash) with replay protection; Application
  Passwords are no longer accepted there. Configure GEORG_BRIDGE_KEYS and
  GEORG_BRIDGE_USER in wp-config.php.
* Require HTTPS (GEORG_BRIDGE_ALLOW_HTTP for local testing).
* The audio upload takes the MP3 as a raw `audio/mpeg` body with query parameters.

= 0.1.1 =
* Reject non-MP3 audio uploaded under an .mp3 name.
* Report oversized uploads as 413 naming the PHP limit.
* Never point two attachments at the same file.
* Keep a published sermon published when updated as a draft.
* Update the attachment title when the audio is unchanged.

= 0.1.0 =
* First release.
