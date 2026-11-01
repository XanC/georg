# georg-bridge

WordPress companion plugin for Georg's `wordpress` output
(`src/georg/modules/outputs/wordpress.py`). See `readme.txt` for the endpoints.

## Install

1. Copy this directory to `wp-content/plugins/georg-bridge/` (no build step) and activate
   *Georg Bridge* under Plugins. Church Content must be active.
2. Create an Application Password (Users > Profile) for an Editor-level user.
3. Configure the output, e.g.

   ```toml
   [[outputs]]
   name = "website"
   type = "wordpress"
   renditions = ["sermon"]
   transcript = true
   options = { base_url = "https://www.example.org", username = "georg", app_password = "env:WP_APP_PASSWORD" }
   ```

   New sermons are created as drafts unless `status` is set to `private` or `publish`.

## Lint

`php -l georg-bridge.php` (CI runs it on every `.php` file under `wordpress/`). For
WordPress coding standards, run `phpcs --standard=WordPress` if you have WPCS installed.
