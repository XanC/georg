=== Georg Bridge ===
Contributors: georg
Tags: sermons, church, rest-api
Requires at least: 5.6
Tested up to: 7.1
Requires PHP: 7.4
Stable tag: 0.1.0
License: AGPL-3.0-only
License URI: https://www.gnu.org/licenses/agpl-3.0.html

REST endpoints that let Georg publish sermons into the Church Content plugin.

== Description ==

Georg (https://github.com/XanC/georg) turns worship-service recordings into published
sermons. This plugin is its WordPress side. It adds the REST namespace `georg/v1`:

* `POST /wp-json/georg/v1/audio` - multipart upload of one MP3 (field `file`) with
  `publication_key`, optional `filename` and `title`. The file is stored as
  `wp-content/uploads/sermons/<sanitized filename>` (an existing file with different
  content is never overwritten; a unique name is used instead) and registered as a
  media-library attachment with `_wp_attachment_metadata`. Repeating the request with the
  same key and content returns the same attachment; new content for the same key is
  stored as a new file and the attachment is pointed at it (the old file is left in
  place, since copies elsewhere, e.g. a CDN, may still reference it).
* `GET /wp-json/georg/v1/sermons/<publication_key>` - the `ctc_sermon` post carrying this
  key in post meta `_georg_publication_key`, or 404.
* `PUT /wp-json/georg/v1/sermons/<publication_key>` - JSON body with `title`, optional
  `content`, `status` (`draft`, `private`, `publish`; default `draft`), `date`,
  `date_gmt`, `terms` (taxonomy => list of names; missing terms are created) and
  `audio_attachment_id`. Creates the sermon or updates the one with this key, sets
  `_ctc_sermon_audio` to the attachment URL and attaches the audio to the post.

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

Security plugins such as Wordfence must allow Application Passwords.

== Changelog ==

= 0.1.0 =
* First release.
