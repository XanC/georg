# georg-bridge

WordPress companion plugin for Georg's `wordpress` output
(`src/georg/modules/outputs/wordpress.py`). See `readme.txt` for the endpoints and
`docs/wordpress.md` for setup, key rotation and the signing protocol.

## Install

1. Copy this directory to `wp-content/plugins/georg-bridge/` (no build step) and activate
   *Georg Bridge* under Plugins. Church Content must be active.
2. Generate a key and add it, with the Editor-level user Georg acts as, to
   `wp-config.php` (never to a repository):

   ```sh
   python -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode())"
   ```

   ```php
   define( 'GEORG_BRIDGE_KEYS', array( '2026-11' => '<key>' ) );
   define( 'GEORG_BRIDGE_USER', 'scripts' );
   ```
3. Put the key in Georg's environment as `GEORG_WP_KEY` and configure the output, e.g.

   ```toml
   [[outputs]]
   name = "website"
   type = "wordpress"
   renditions = ["sermon"]
   transcript = true
   options = { base_url = "https://www.example.org", key_id = "2026-11", key = "env:GEORG_WP_KEY" }
   ```

   New sermons are created as drafts unless `status` is set to `private` or `publish`.

## Test

`php tests/signing-test.php` checks the signing code against `tests/vectors.json` (also
used by Georg's Python tests) and replay, expiry, tampering and rotation cases.
`docs/wordpress-testing.md` runs the plugin and Georg's output against a local Docker
WordPress with Church Content.

## Lint

`php -l georg-bridge.php` (CI runs it on every `.php` file under `wordpress/`). For
WordPress coding standards, run `phpcs --standard=WordPress` if you have WPCS installed.
