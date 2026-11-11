# wp-authority

Authority control for the Church Content sermon taxonomies: `ctc_sermon_book`
(Scripture), `ctc_sermon_series` (liturgical day) and `ctc_sermon_speaker` (preacher).
It reads and writes only through WP-CLI, so the site needs no plugin endpoints.

Nothing changes on the site until you run `apply` on a plan you've reviewed, and `apply`
always writes a full backup first.

## Running

From the repository root:

```sh
export PYTHONPATH=contrib/wp-authority
alias wpa='uv run python -m wp_authority'

# Local WP-CLI:
wpa --path /var/www/html plan --mapping mapping.toml --out-dir out/
# Remote: runs `wp ... --format=json` (reads) and `wp eval-file -` (writes) over ssh.
wpa --ssh church --path /var/www/html --wp "sudo -u www-data wp" plan --mapping mapping.toml
```

`--wp` is the WP-CLI command (default `wp`); for a phar, use something like
`--wp "sudo -u www-data php /root/wp-cli.phar"`.

| Command | Does |
| --- | --- |
| `dump OUT.json` | Writes a snapshot: every term (id, name, slug, parent, description) and every sermon's assignments, in all post statuses including trash. |
| `dump-rest URL OUT.json` | Read-only snapshot from the public REST API (`/wp-json/wp/v2/ctc_sermon` and the three taxonomies), for planning before WP-CLI is installed. It sees published sermons only, and only the terms they use plus whatever the term listings return (term paging skips terms, so missing IDs are fetched with `?include=`). Use it to review a plan; `apply` still needs a plan made from a WP-CLI `dump`. |
| `plan [--mapping M] [--snapshot S] [--out-dir D]` | Writes `plan.json` and `plan.csv`. With `--snapshot`, plans from a dump instead of the site. Read-only. |
| `apply plan.json [--backup-dir D]` | Writes `backup-<timestamp>.json` (a full snapshot), then applies the plan's actions and writes `apply-<timestamp>.json` with each action's result. |
| `restore backup.json` | Puts the backup back (see below). |

The workflow: copy `mapping.example.toml` to `mapping.toml`, run `plan`, read
`plan.csv`, edit the mapping (copy suggestions into it, fix the "needs a human" terms),
and repeat until the plan is right. Then `apply`.

## Rules

**Scripture.** Composite terms are split into one term per reference. A segment (between
`,` or `;`) that starts with a book name starts a new reference; verse (`16-21`) and
chapter (`2:18-26`) continuations stay with the reference before them, so
`"Luke 12:13-21, Ecclesiastes 1:2, 12-14, 2:18-26"` becomes `Luke 12:13-21` and
`Ecclesiastes 1:2, 12-14, 2:18-26`. Book names use Georg's parser
(`georg.modules.processors.metadata.references`), so `I John` becomes `1 John`. Dashes
become `dash` (`"-"` by default, or `"–"`) without surrounding spaces; HTML entities and
UTF-8 mojibake (`John 6:22â35`) are decoded. Anything else (`?`, `_`, a liturgical day
in the book taxonomy, a missing book or chapter) goes to **needs a human** and is left
alone until `[scripture.fix]` says what it should become. With `nest_under_books`, each
reference term gets a parent term named exactly as its book.

**Series and speakers.** `[series.canonical]` / `[speakers.canonical]` map a canonical name
to aliases; sermons on an alias move to the canonical term. **Suggested merges** (in/of,
"the", St/St., numeric vs spelled-out ordinals, case, punctuation; for speakers, spacing
like "De Young"/"DeYoung") are listed separately and are not applied until copied into
the mapping. Suggested canonical names spell ordinals out ("Third Sunday of Advent").
Speakers keep the title they had at the time; `[speakers.people]` instead nests a
person's speaker terms under a parent term named for the person, and the plan suggests
such groups; `{ terms = [...], description = "..." }` also sets the parent's
description. To rename everyone to their current title instead, use plain
`[speakers.canonical]` merges. Both taxonomies are hierarchical, so the parent's archive page lists the
children's sermons (WordPress's taxonomy queries include children by default).

**Term identity.** Each target name resolves to an existing term with exactly that name;
otherwise the most-used term being renamed to it (so its slug and ID survive); otherwise a
new term. Terms no sermon uses afterwards are proposed for deletion (`[unused]`).

**Series for sermons without one.** For each sermon with no series, the plan takes the
liturgical days of its date under the LCMS three-year calendar (clue 1), and checks
whether the sermon's Scripture shares a verse with that day's reading in that year's
series (A, B or C) of the lectionary, or whether its title is "Sermon for [the] X" naming that day (clue 2). It
proposes a series only when exactly one day has both clues; otherwise the sermon goes to
"needs a human" with the candidate days. Proposals are never applied: copy the ones you
accept into `[series.assign]`.

The calendar (`wp_authority/liturgical.py`): Easter by the Meeus/Jones/Butcher algorithm;
Advent 1 is the fourth Sunday before Christmas; the church year ending in 2017 was Series
A. Day names are LSB's with spelled-out ordinals ("Third Sunday after the Epiphany",
"Twelfth Sunday after Pentecost"), with common variants as alternative names ("Advent
III", "Second-to-Last Sunday of the Church Year", "Christ the King") so existing terms are
found; the canonical table decides which name a new term gets. Days kept on weekdays are
Christmas Eve and Day, the Epiphany, Ash Wednesday, Holy Week, and the Ascension; other
weekday sermons have no candidate day. Fixed feasts count when they fall on a Sunday; All
Saints' Day is also observed on the first Sunday of November and Reformation Day on the
last Sunday of October.

`check-calendar [--snapshot S] [--skip-church-year YEAR]` compares the calendar with
every sermon that already has a series and writes the agreement rate and the
disagreements to `calendar-check.json`.

## Apply and restore

`apply` checks each action against the live state: creates reuse a term of the same
name, renames keep the slug, a sermon is reassigned only if its terms are still what the
plan saw (otherwise it is skipped and reported), and a term is deleted only if nothing
uses it and it has no children. Running the same plan twice changes nothing the second
time.

`restore` recreates deleted terms with their original IDs (when the ID and slug are
free), resets names, slugs, descriptions and parents, puts every backed-up sermon's
assignments back, and deletes terms the backup didn't have if nothing uses them.

## Lectionary data

`wp_authority/data/lectionary.json` holds the reading references (no Bible text) of the
Lutheran Service Book three-year lectionary (Series A, B and C) and its feasts and
festivals. It was extracted by `tools/build_lectionary.py`
from The Wartburg Project's LSB lectionary pages,
<https://wartburgproject.org/resources/lectionaries/lsb> (e.g.
[3-year Series B](https://wartburgproject.org/resources/lectionaries/lsb/3year-b),
[feasts and occasions](https://wartburgproject.org/resources/lectionaries/lsb/occasions)),
which follow *Lutheran Service Book* (Concordia Publishing House, 2006); the LCMS
publishes the same lectionary at <https://www.lcms.org/sslpage.aspx?pid=448>.
Three-year Sundays after Pentecost are looked up by Proper number (Proper 4 = the Sunday
between May 29 and June 4, and so on). Rerun the script to refresh the data.

## Tests

`uv run pytest contrib/wp-authority` runs the splitter, calendar and plan tests. The
apply/restore round trip runs against the throwaway WordPress of `scripts/wordpress-e2e`:

```sh
scripts/wordpress-e2e/setup.sh
WP_AUTHORITY_WP="docker compose -f $PWD/scripts/wordpress-e2e/compose.yaml --progress quiet run --rm -T cli wp" \
WP_AUTHORITY_URL=http://localhost:8080 \
uv run pytest contrib/wp-authority/tests/test_wpa_wordpress.py
```
