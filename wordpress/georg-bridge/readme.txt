=== Georg Bridge ===
Contributors: georg
Tags: sermons, church, rest-api
Requires at least: 5.6
Tested up to: 7.1
Requires PHP: 7.4
Stable tag: 0.1.1
License: AGPL-3.0-only
License URI: https://www.gnu.org/licenses/agpl-3.0.html

REST endpoints that let Georg publish sermons into the Church Content plugin.

== Description ==

Georg (https://github.com/XanC/georg) turns worship-service recordings into published
sermons. This plugin is its WordPress side. It adds the REST namespace `georg/v1`:

* `POST /wp-json/georg/v1/audio` - multipart upload of one MP3 (field `file`) with
  `publication_key`, optional `filename` and `title`. The file is stored as
  `wp-content/uploads/sermons/<sanitized filename>` (an existing file with different
  content, or one used by another attachment, is never reused; a unique name is chosen
  instead) and registered as a media-library attachment with `_wp_attachment_metadata`
  (length, length_formatted, filesize, mime_type, bitrate, ...). The file must really be
  an MP3: other audio formats named `.mp3` are rejected with 400. A file over PHP's
  `upload_max_filesize` or a request over `post_max_size` is rejected with 413. Repeating the request with the
  same key and content returns the same attachment; new content for the same key is
  stored as a new file and the attachment is pointed at it (the old file is left in
  place, since copies elsewhere, e.g. a CDN, may still reference it).
* `GET /wp-json/georg/v1/sermons/<publication_key>` - the `ctc_sermon` post carrying this
  key in post meta `_georg_publication_key`, or 404.
* `PUT /wp-json/georg/v1/sermons/<publication_key>` - JSON body with `title`, optional
  `content`, `status` (`draft`, `private`, `publish`; default `draft`), `date`,
  `date_gmt`, `terms` (taxonomy => list of names; missing terms are created) and
  `audio_attachment_id`. Creates the sermon or updates the one with this key, sets
  `_ctc_sermon_audio` to the attachment URL and attaches the audio to the post. An update
  asking for `draft` leaves a sermon that has since been published, scheduled, or made
  private in that status; `private` and `publish` are always applied.

Requests must be authenticated with an Application Password of a user who can edit
others' posts and upload files (`publish` also needs the sermon type's publish
capability). The `georg_bridge_permission` filter can change this check.

The plugin requires Church Content (post type `ctc_sermon`).

== Installation ==

1. Copy the `georg-bridge` directory to `wp-content/plugins/` and activate it.
2. Create or pick a user with the Editor role (or another role with `edit_others_posts`
   and `upload_files`), and add an Application Password under Users > Profile.
3. Give Georg the site URL, the user name, and the application password (as an
   environment variable referenced by `app_password = "env:NAME"`).

Security plugins such as Wordfence must allow Application Passwords. PHP's
`upload_max_filesize` and `post_max_size` must exceed the largest sermon MP3.

== Changelog ==

= 0.1.1 =
* Reject non-MP3 audio uploaded under an .mp3 name.
* Report oversized uploads as 413 naming the PHP limit.
* Never point two attachments at the same file.
* Keep a published sermon published when updated as a draft.
* Update the attachment title when the audio is unchanged.

= 0.1.0 =
* First release.
