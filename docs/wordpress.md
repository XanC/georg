# WordPress output and georg-bridge

Georg publishes sermons to WordPress through the `georg-bridge` plugin
(`wordpress/georg-bridge/`) and the `wordpress` output
(`src/georg/modules/outputs/wordpress.py`). The plugin's endpoints are listed in
`wordpress/georg-bridge/readme.txt`; local testing is in `docs/wordpress-testing.md`.

Every request to the plugin's `georg/v1` routes is signed with a per-installation shared
secret (HMAC-SHA256). That is the only authentication those routes accept: Application
Passwords, Basic auth and logged-in cookies are refused there. The signature does not log
anyone in to WordPress; it is checked only by the `georg/v1` permission callbacks, so the
key cannot be used against any other endpoint.

## Setup

1. Generate a key (32 random bytes, base64url) on a trusted machine:

   ```sh
   python -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode())"
   ```

2. Add it to the site's `wp-config.php` (above "That's all, stop editing"), never to a
   repository:

   ```php
   define( 'GEORG_BRIDGE_KEYS', array( '2026-11' => '<the key>' ) ); // key id => key
   define( 'GEORG_BRIDGE_USER', 'scripts' ); // WordPress login Georg acts as
   ```

   The key id is any label matching `[A-Za-z0-9._-]{1,64}`; a date makes rotation easy.
   `GEORG_BRIDGE_USER` must be a user that can `edit_others_posts` and `upload_files`
   (Editor). Georg never names the user; the site decides. If the constants are missing,
   a key is not valid base64url of at least 32 bytes, or the user does not exist, every
   `georg/v1` request fails with 503 "georg-bridge is not configured".

3. Give Georg the same key through its environment and reference it in the output:

   ```toml
   options = { base_url = "https://www.example.org", key_id = "2026-11", key = "env:GEORG_WP_KEY" }
   ```

   `base_url` must be `https://`. `allow_insecure_http = true` permits `http://`, and the
   plugin likewise accepts plain HTTP only with `define( 'GEORG_BRIDGE_ALLOW_HTTP', true );`;
   both are for local testing only.

## Post status and full text

The `wordpress` output creates drafts unless `status = "publish"` (or `"private"`) is set.
With `full_text = true` (the default), a sermon whose post content is a non-empty
transcript also gets Church Content's "Full sermon text provided" box ticked (post meta
`_ctc_sermon_has_full_text` = `1`, georg-bridge 0.3.0 or later). With `full_text = false`,
Georg leaves that box alone.

## Rotating the key

1. Generate a new key and add it next to the old one:
   `array( '2026-11' => '<old>', '2027-05' => '<new>' )`. Both are accepted.
2. Set Georg's `GEORG_WP_KEY` to the new key and `key_id` to `2027-05`, and restart it.
3. Remove `'2026-11'` from `GEORG_BRIDGE_KEYS`. Requests signed with it now get 401.

If a key may have leaked, skip the overlap: replace it in `wp-config.php` first, then
update Georg.

## Signing protocol

Each request carries five headers:

| Header | Value |
| --- | --- |
| `X-Georg-Key-Id` | key id, `[A-Za-z0-9._-]{1,64}` |
| `X-Georg-Timestamp` | Unix time in seconds, decimal |
| `X-Georg-Nonce` | a fresh random value per request; Georg sends `secrets.token_hex(16)`, the server accepts `[A-Za-z0-9_-]{22,128}` |
| `X-Georg-Content-SHA256` | lowercase hex SHA-256 of the exact raw body (empty body: the hash of the empty string) |
| `X-Georg-Signature` | lowercase hex HMAC-SHA256 of the canonical string, keyed with the decoded key bytes |

The canonical string is these lines joined by `\n` (no trailing newline):

```
GEORG-HMAC-SHA256
<key id>
<timestamp>
<nonce>
<HTTP method, uppercase>
<REST route, e.g. /georg/v1/sermons/georg:stpaul:sermon-1:wordpress>
<canonical query>
<content sha256>
```

- The route is the path after the REST prefix (`/wp-json`), percent-decoded; the server
  uses `WP_REST_Request::get_route()`.
- The canonical query is every query parameter except `rest_route`, sorted by name and
  then value (byte order), each written `rawurlencode(name)=rawurlencode(value)` (Python:
  `quote(x, safe="")`) and joined by `&`; empty if there are none. Array parameters are
  rejected with 400.

The server checks, in order, and stops at the first failure:

1. HTTPS (`is_ssl()`), unless `GEORG_BRIDGE_ALLOW_HTTP`: else 403.
2. All headers present and well formed, key id configured: else 401 "invalid signature".
3. Timestamp within 300 seconds of the server clock, either way: else 401 "request expired".
4. The body's SHA-256 equals `X-Georg-Content-SHA256`: else 401 "invalid signature".
5. The signature equals the expected HMAC (`hash_equals`): else 401 "invalid signature".
6. The nonce has not been used with this key: it is recorded atomically
   (`INSERT IGNORE` of `_georg_nonce_<sha256(key id:nonce)>` into the options table), and
   a nonce already present gets 401 "replayed request". Records older than 600 seconds are
   purged on later successful requests; by then their timestamps are rejected anyway.
7. The configured user exists (else 503), becomes the current user, and must have the
   operation's capabilities: `edit_others_posts` for the sermon routes, plus
   `upload_files` for audio (else 403). The `georg_bridge_permission` filter runs after
   this check.

Signature errors are deliberately generic, and neither side logs headers, keys or
signatures. `wordpress/georg-bridge/tests/vectors.json` has worked examples that both
the PHP and the Python implementations are tested against.

Audio is uploaded as a raw body (`Content-Type: audio/mpeg`) with `publication_key`,
`filename` and `title` as query parameters, so all of it is signed. (PHP does not expose
a multipart body, so it could not be hashed.)

## Retries and idempotency

Georg signs every attempt afresh, retries included, with a new timestamp and nonce, so
a retry is never mistaken for a replay. Retries cannot duplicate anything: the audio
upload and the sermon `PUT` are both keyed by the publication key, so repeating either
updates the same attachment or post. A retry after a lost response therefore leaves
exactly one sermon and one attachment.

## Edits made on the website

georg-bridge 0.4.0 keeps edits made in WordPress from being overwritten by a republish.

- After every sermon write the bridge stores `_georg_written` post meta: a sha256 per
  field (`title`, `content`, `date`, and `terms:<taxonomy>` with sorted term IDs for each
  taxonomy Georg sets) of the values as WordPress saved them, read back from the saved
  post, plus `written_at`.
- The sermon `PUT` takes optional `fields` (which of those fields, plus `audio` and
  `full_text`, to write; all when absent) and `overwrite`. If a requested field's current
  fingerprint differs from `_georg_written` and the field isn't in `overwrite`, the bridge
  writes nothing and returns 409 `georg_edited_on_site` with `fields`, `modified_gmt` and
  `modified_by` (display name of `_edit_last` if that isn't the bridge user, else of the
  latest revision's author).
- Every sermon write sets `_edit_last` to the bridge user, so WordPress's own "last
  edited by" stays meaningful.
- Sermons written before 0.4.0 have no `_georg_written`. They count as edited in every
  field (nothing says which field changed) when `_edit_last` is a user other than the
  bridge user and `post_modified_gmt` is more than 10 seconds after `georg_written_at`.
  Georg sends `georg_written_at` (RFC 3339, from `publications.published_at`, the time
  the website last accepted a write) on the `PUT` and on `/status`. Without it, a
  non-bridge `_edit_last` alone counts as an edit. Revisions can't decide this: the
  editor's save need not leave a revision (post 8228 had only an autosave).
- Signed `GET /georg/v1/sermons/<key>/status` returns `id`, `link`, `status`, `edited`
  (fields edited on the website), `written_at`, `modified_gmt`, `modified_by` and the
  bridge `version`, without changing anything.

On Georg's side, a republish sends only the fields that differ between the revision the
website last accepted (`publications.published_revision_id`) and the one being published;
the audio is uploaded only when its artifact changed. A first publish sends everything.
A 409 puts the website publication in the `conflict` state with the fields and editor
("Website edited: needs decision" on the review page); it is not retried, and the other
outputs publish as usual.

Once a website holds the sermon, approving a new revision publishes the other outputs
only. The review page's "Update website…" shows which fields would be sent and, if some
were edited on the website, "Content was edited on the website (date, by name)" with
"Skip these fields" (send the rest) or "Overwrite on website" (needs a confirming tick).

Install georg-bridge 0.4.0 before deploying a Georg that sends `fields`: an older bridge
ignores `fields` and would write every field. Georg also sends `georg_written_at`, which
0.4.0 ignores; install 0.4.1 for the pre-0.4.0 rule above.

## Threat notes

- **Clock sync.** The Georg host and the web server must keep time with NTP; more than
  five minutes of drift makes every request fail as expired.
- **HTTPS.** The signature protects integrity and authenticity, not confidentiality, and
  a request captured in transit could be replayed to a different site using the same key.
  HTTPS is therefore required. Use a different key for every site.
- **Replay window.** A captured request is refused after five minutes (timestamp) and
  before that (nonce). Nonces are stored per site, so the same key on two sites would let a
  request be replayed across them; another reason to use one key per site.
- **Key storage.** The key is as powerful as the configured user on `georg/v1` and
  nothing else. Keep it only in `wp-config.php` and Georg's environment or secret store,
  never in a repository, ticket or log. Georg's error messages never include it.
- **Large bodies.** Audio is read into memory to hash it; WordPress's maximum upload size
  (the smaller of `upload_max_filesize` and `post_max_size`) caps it, with 413 above that.

## Same-date warning on the review page

When a `wordpress` output is configured, the sermon review page asks the site's core REST
API (`/wp-json/wp/v2/ctc_sermon?after=…&before=…`, public and unsigned, so published posts
only) for sermons dated on the service date. Any post that isn't this sermon's own
publication is shown above the Publish button ("Published outside Georg for this date:
…"). Publishing is not blocked. If the site can't be reached, the page still loads
with a note that the check couldn't be done.

The same warning appears on a service page before a sermon is proposed, and the Services
list shows a "Published outside Georg" badge linking to the existing post. The list makes one
paginated request for the date range of all listed services. Lookups, including failures,
are cached in the web process for 60 s. If the lookup fails, the list shows no badges.
