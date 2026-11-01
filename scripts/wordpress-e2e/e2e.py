# SPDX-License-Identifier: AGPL-3.0-only
"""End-to-end check of georg-bridge and the `wordpress` output against the docker site.

Run `scripts/wordpress-e2e/setup.sh` first, then `uv run python scripts/wordpress-e2e/e2e.py`.
Exits non-zero if any check fails. See docs/wordpress-testing.md.
"""

import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any, cast

import httpx

from georg.core.config import OutputConfig
from georg.core.errors import ModuleError
from georg.core.types import ArtifactRef, PublicationRequest, SermonMetadata
from georg.modules.outputs.wordpress import WordPressOutput

HERE = Path(__file__).resolve().parent
RUN = str(int(time.time()))
SAFE_NAME = re.compile(r"sermons/[A-Za-z0-9._-]+\.mp3")
failures: list[str] = []


def check(ok: bool, what: str, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + what + (f": {detail}" if detail != "" and not ok else ""))
    if not ok:
        failures.append(what)


def env() -> dict[str, str]:
    values = dict(os.environ)
    for line in (HERE / ".env.e2e").read_text().splitlines():
        name, _, value = line.partition("=")
        values.setdefault(name, value)
    return values


ENV = env()
BASE = ENV["GEORG_WP_URL"]
API = f"{BASE}/wp-json/georg/v1"


def compose(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["docker", "compose", "--progress", "quiet", *args],
        cwd=HERE,
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def php(code: str) -> Any:
    """Evaluate PHP in WordPress (via wp-cli) and decode what it echoes as JSON."""
    return json.loads(compose("run", "--rm", "-T", "cli", "wp", "eval", code))


def debug_log() -> str:
    return compose("exec", "-T", "wordpress", "sh", "-c", "cat wp-content/debug.log 2>/dev/null")


def clear_debug_log() -> None:
    compose("exec", "-T", "wordpress", "sh", "-c", ": > wp-content/debug.log")


def mp3(path: Path, seconds: float, freq: int = 440, bitrate: str = "64k") -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}",
            "-ac",
            "1",
            "-b:a",
            bitrate,
            str(path),
        ],
        check=True,
    )
    return path


def artifact(path: Path, seconds: float) -> ArtifactRef:
    data = path.read_bytes()
    return ArtifactRef(
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        mime="audio/mpeg",
        duration_ms=int(seconds * 1000),
    )


def output(
    status: str = "draft", user: str = "georg", password_var: str = "EDITOR"
) -> WordPressOutput:
    os.environ[f"GEORG_WP_{password_var}_PASSWORD"] = ENV[f"GEORG_WP_{password_var}_PASSWORD"]
    return WordPressOutput(
        OutputConfig(
            name="website",
            type="wordpress",
            renditions=("sermon",),
            transcript=True,
            options={
                "base_url": BASE,
                "username": user,
                "app_password": f"env:GEORG_WP_{password_var}_PASSWORD",
                "status": status,
                "timezone": "America/Chicago",
                "service_time": "10:30",
                "content_prefix": "[Machine transcription]",
                "attempts": 1,
            },
        )
    )


def request(key: str, audio: ArtifactRef, title: str, transcript: str) -> PublicationRequest:
    return PublicationRequest(
        publication_key=key,
        service_date=date(2026, 11, 1),
        metadata=SermonMetadata(
            title=title,
            preacher="Rev. Jane O'Neil & Guest",
            scripture=("Matthew 5:1-12",),
            occasion="All Saints' Day",
            service_date=date(2026, 11, 1),
        ),
        transcript_markdown=transcript,
        files={"sermon": audio},
    )


def sermon_state(post_id: int) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        php(f"""
        $p = get_post({post_id});
        $audio = get_post_meta($p->ID, '_ctc_sermon_audio', true);
        $att = attachment_url_to_postid($audio);
        $terms = array();
        foreach (array('ctc_sermon_speaker','ctc_sermon_book','ctc_sermon_series') as $t)
            $terms[$t] = wp_get_object_terms($p->ID, $t, array('fields' => 'names'));
        $a = get_post($att);
        echo wp_json_encode(array(
            'type' => $p->post_type, 'status' => $p->post_status, 'title' => $p->post_title,
            'content' => $p->post_content, 'date' => $p->post_date, 'date_gmt' => $p->post_date_gmt,
            'key' => get_post_meta($p->ID, '_georg_publication_key', true), 'terms' => $terms,
            'audio' => $audio, 'att' => $att,
            'att_type' => $a ? $a->post_type : null, 'att_mime' => $a ? $a->post_mime_type : null,
            'att_parent' => $a ? $a->post_parent : null, 'att_guid' => $a ? $a->guid : null,
            'att_status' => $a ? $a->post_status : null, 'att_title' => $a ? $a->post_title : null,
            'att_key' => get_post_meta($att, '_georg_publication_key', true),
            'att_file' => get_post_meta($att, '_wp_attached_file', true),
            'att_path' => get_attached_file($att),
            'att_exists' => file_exists(get_attached_file($att)),
            'att_meta' => wp_get_attachment_metadata($att),
            'upload_url' => wp_upload_dir()['baseurl'],
            'upload_dir' => wp_upload_dir()['basedir'],
        ));
    """),
    )


def count_with_key(key: str) -> dict[str, int]:
    return cast(
        dict[str, int],
        php(f"""
        $q = function ($type) {{ return count(get_posts(array('post_type' => $type,
            'post_status' => 'any', 'posts_per_page' => -1, 'fields' => 'ids',
            'meta_key' => '_georg_publication_key', 'meta_value' => {json.dumps(key)}))); }};
        echo wp_json_encode(array('sermons' => $q('ctc_sermon'),
            'attachments' => $q('attachment')));
    """),
    )


def check_sermon(
    state: dict[str, Any], *, key: str, title: str, words: str, seconds: int, status: str = "draft"
) -> None:
    check(state["type"] == "ctc_sermon", "post type is ctc_sermon", state["type"])
    check(state["status"] == status, f"post status is {status}", state["status"])
    check(state["title"] == title, "post title", state["title"])
    check(state["content"].startswith("<p>[Machine transcription]</p>"), "content prefix")
    check(words in state["content"], "transcript in content", state["content"][:200])
    check(state["date"] == "2026-11-01 10:30:00", "post_date is local service time", state["date"])
    check(
        state["date_gmt"] == "2026-11-01 16:30:00", "post_date_gmt is UTC (CST)", state["date_gmt"]
    )
    check(state["key"] == key, "sermon _georg_publication_key", state["key"])
    expected_terms = {
        "ctc_sermon_speaker": ["Rev. Jane O'Neil & Guest"],
        "ctc_sermon_book": ["Matthew 5:1-12"],
        "ctc_sermon_series": ["All Saints' Day"],
    }
    decoded = {t: [html.unescape(n) for n in names] for t, names in state["terms"].items()}
    check(decoded == expected_terms, "terms", state["terms"])
    url_re = re.escape(state["upload_url"]) + r"/sermons/[^/]+\.mp3"
    check(
        re.fullmatch(url_re, state["audio"] or "") is not None,
        "_ctc_sermon_audio under uploads/sermons/",
        state["audio"],
    )
    check(
        state["att"] > 0 and state["att_type"] == "attachment",
        "audio URL resolves to an attachment",
        state["att"],
    )
    check(state["att_mime"] == "audio/mpeg", "attachment mime", state["att_mime"])
    check(state["att_status"] == "inherit", "attachment status inherit", state["att_status"])
    check(
        state["att_guid"] == state["audio"], "attachment guid equals audio URL", state["att_guid"]
    )
    check(
        state["att_parent"] == int(state["att_parent"] or 0) and state["att_parent"] > 0,
        "attachment parented to sermon",
        state["att_parent"],
    )
    check(state["att_key"] == key, "attachment _georg_publication_key", state["att_key"])
    check(
        re.fullmatch(r"sermons/[^/]+\.mp3", state["att_file"] or "") is not None,
        "_wp_attached_file is sermons/<file>",
        state["att_file"],
    )
    check(
        state["att_exists"] and state["att_path"].startswith(state["upload_dir"] + "/sermons/"),
        "file exists in uploads/sermons/",
        state["att_path"],
    )
    meta = state["att_meta"] or {}
    check(abs(int(meta.get("length", -1)) - seconds) <= 1, "metadata length", meta.get("length"))
    check(
        meta.get("length_formatted") == f"0:{seconds:02d}",
        "metadata length_formatted",
        meta.get("length_formatted"),
    )
    check(meta.get("mime_type") == "audio/mpeg", "metadata mime_type", meta.get("mime_type"))
    check(meta.get("dataformat") == "mp3", "metadata dataformat mp3", meta.get("dataformat"))
    check(
        isinstance(meta.get("filesize"), int) and meta["filesize"] > 0,
        "metadata filesize",
        meta.get("filesize"),
    )


def bridge_post_audio(
    user: str,
    password: str,
    *,
    key: str,
    path: Path,
    filename: str,
    content_type: str = "audio/mpeg",
) -> httpx.Response:
    with path.open("rb") as fh:
        return httpx.post(
            f"{API}/audio",
            auth=(user, password),
            data={"publication_key": key, "filename": filename},
            files={"file": (filename, fh, content_type)},
            timeout=120,
        )


def post_ids(post_type: str) -> list[int]:
    query = f"array('post_type' => '{post_type}', 'post_status' => 'any', 'fields' => 'ids',"
    return list(php(f"echo wp_json_encode(get_posts({query} 'posts_per_page' => -1)));"))


def page_loads() -> None:
    with httpx.Client(base_url=BASE, follow_redirects=True, timeout=60) as admin:
        admin.post(
            "/wp-login.php",
            data={"log": "admin", "pwd": "admin", "testcookie": "1"},
            cookies={"wordpress_test_cookie": "WP Cookie check"},
        )
        sermon_ids = post_ids("ctc_sermon")
        attachment_ids = post_ids("attachment")
        paths = [
            "/wp-admin/",
            "/wp-admin/plugins.php",
            "/wp-admin/edit.php?post_type=ctc_sermon",
            "/wp-admin/upload.php",
            "/wp-admin/upload.php?mode=list",
            "/wp-admin/edit-tags.php?taxonomy=ctc_sermon_speaker&post_type=ctc_sermon",
        ]
        paths += [f"/wp-admin/post.php?post={i}&action=edit" for i in sermon_ids + attachment_ids]
        previews = php(
            f"echo wp_json_encode(array_map('get_preview_post_link', {sermon_ids!r}));".replace(
                "[", "array("
            ).replace("]", ")")
        )
        paths += [url.removeprefix(BASE) for url in previews]
        for path in paths:
            r = admin.get(path)
            check(
                r.status_code == 200 and "wp-login.php" not in str(r.url),
                f"admin GET {path}",
                r.status_code,
            )
    with httpx.Client(base_url=BASE, follow_redirects=True, timeout=60) as anon:
        for path in ["/", "/sermons/", "/feed/", "/?feed=rss2&post_type=ctc_sermon", "/wp-json/"]:
            r = anon.get(path)
            check(r.status_code == 200, f"front GET {path}", r.status_code)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="georg-wp-e2e-"))
    clear_debug_log()
    for action in ("deactivate", "activate"):
        compose("run", "--rm", "-T", "cli", "wp", "plugin", action, "georg-bridge")
    log = debug_log()
    check(log.strip() == "", "plugin activation logs nothing", log[-2000:])
    editor_pw = ENV["GEORG_WP_EDITOR_PASSWORD"]
    key = f"georg:e2e{RUN}:sermon-1:website"

    # First publication: draft sermon with audio.
    audio = artifact(mp3(tmp / "a.mp3", 30), 30)
    out = output()
    check(out.find_existing(key) is None, "find_existing before publish is None")
    ref = out.publish(
        request(
            key,
            audio,
            "Blessed Are",
            "Blessed are the poor in spirit.\n\nFor theirs is the kingdom.",
        )
    )
    post_id = int(ref.external_id)
    state = sermon_state(post_id)
    check_sermon(
        state, key=key, title="Blessed Are", words="<p>For theirs is the kingdom.</p>", seconds=30
    )
    first_att, first_file = state["att"], state["att_file"]
    found = out.find_existing(key)
    check(
        found is not None and found.external_id == ref.external_id,
        "find_existing returns the post",
        found,
    )
    check(found is not None and str(found.url).startswith(BASE), "find_existing has a link", found)

    # Republish, same audio: same post and attachment, new title/transcript.
    ref2 = out.publish(request(key, audio, "Blessed Are the Meek", "Blessed are the meek."))
    state2 = sermon_state(post_id)
    check(ref2.external_id == ref.external_id, "republish updates the same post", ref2)
    check_sermon(
        state2,
        key=key,
        title="Blessed Are the Meek",
        words="<p>Blessed are the meek.</p>",
        seconds=30,
    )
    check("kingdom" not in state2["content"], "old transcript replaced")
    check(
        state2["att"] == first_att and state2["att_file"] == first_file,
        "same audio reuses attachment and file",
        (state2["att"], state2["att_file"]),
    )
    check(
        state2["att_title"] == "Blessed Are the Meek",
        "attachment title updated",
        state2["att_title"],
    )
    check(
        count_with_key(key) == {"sermons": 1, "attachments": 1},
        "no duplicates after republish",
        count_with_key(key),
    )

    # Republish with new audio: same attachment, new file, refreshed metadata.
    audio3 = artifact(mp3(tmp / "b.mp3", 20, freq=660), 20)
    ref3 = out.publish(request(key, audio3, "Blessed Are the Meek", "Blessed are the meek."))
    state3 = sermon_state(post_id)
    check(ref3.external_id == ref.external_id, "new audio keeps the same post", ref3)
    check(state3["att"] == first_att, "new audio keeps the same attachment", state3["att"])
    check(state3["att_file"] != first_file, "new audio stored as a new file", state3["att_file"])
    check_sermon(state3, key=key, title="Blessed Are the Meek", words="meek", seconds=20)
    check(
        count_with_key(key) == {"sermons": 1, "attachments": 1},
        "no duplicates after new audio",
        count_with_key(key),
    )

    # Private status.
    pkey = f"georg:e2e{RUN}:sermon-2:website"
    pref = output("private").publish(request(pkey, audio, "Private One", "Hidden."))
    check(
        sermon_state(int(pref.external_id))["status"] == "private",
        "status=private creates a private post",
    )

    # A sermon someone published after review stays published when Georg updates it as a draft.
    compose(
        "run", "--rm", "-T", "cli", "wp", "post", "update", str(post_id), "--post_status=publish"
    )
    out.publish(request(key, audio3, "Blessed Are the Meek", "Blessed are the meek, revised."))
    state4 = sermon_state(post_id)
    check(
        state4["status"] == "publish",
        "draft republish keeps a published sermon published",
        state4["status"],
    )
    check("revised" in state4["content"], "published sermon content updated")
    output("private").publish(request(key, audio3, "Blessed Are the Meek", "Blessed are the meek."))
    check(
        sermon_state(post_id)["status"] == "private", "explicit private status is applied on update"
    )

    # Users without edit_others_posts (author) or anything (subscriber) are rejected.
    for user, var in (("author1", "AUTHOR"), ("subscriber1", "SUBSCRIBER")):
        try:
            output(user=user, password_var=var).publish(
                request(f"georg:e2e{RUN}:{user}:website", audio, "X", "x")
            )
            check(False, f"{user} rejected")
        except ModuleError as exc:
            check("HTTP 403" in str(exc) and not exc.retryable, f"{user} rejected with 403", exc)
        r = httpx.get(f"{API}/sermons/{key}", auth=(user, ENV[f"GEORG_WP_{var}_PASSWORD"]))
        check(r.status_code == 403, f"{user} GET sermon is 403", r.status_code)
    r = httpx.get(f"{API}/sermons/{key}")
    check(r.status_code == 401, "anonymous GET sermon is 401", r.status_code)
    r = httpx.get(f"{API}/sermons/{key}", auth=("georg", "wrong pass word abcd efgh ijkl"))
    check(r.status_code == 401, "bad application password is 401", r.status_code)
    with httpx.Client() as c:
        c.post(
            f"{BASE}/wp-login.php",
            data={"log": "admin", "pwd": "admin"},
            cookies={"wordpress_test_cookie": "WP Cookie check"},
        )
        nonce = c.get(f"{BASE}/wp-admin/admin-ajax.php?action=rest-nonce").text
        r = c.get(f"{API}/sermons/{key}", headers={"X-WP-Nonce": nonce})
        check(
            r.status_code == 403,
            "cookie-authenticated admin is 403 (application password required)",
            r.status_code,
        )

    # Non-MP3 uploads are rejected.
    (tmp / "evil.php").write_text("<?php echo 'pwned';")
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=2",
            str(tmp / "tone.wav"),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=2",
            "-c:a",
            "libvorbis",
            str(tmp / "tone.ogg"),
        ],
        check=True,
    )
    for name, path, upload_as in (
        ("PHP as .mp3", tmp / "evil.php", "sermon.mp3"),
        ("PHP as .php", tmp / "evil.php", "evil.php"),
        ("WAV as .mp3", tmp / "tone.wav", "tone.mp3"),
        ("Ogg as .mp3", tmp / "tone.ogg", "tone.mp3"),
    ):
        r = bridge_post_audio(
            "georg", editor_pw, key=f"georg:e2e{RUN}:bad:website", path=path, filename=upload_as
        )
        check(r.status_code == 400, f"{name} rejected", (r.status_code, r.text[:200]))
    check(
        count_with_key(f"georg:e2e{RUN}:bad:website")["attachments"] == 0,
        "rejected uploads leave no attachment",
    )

    # Path traversal in the file name stays inside uploads/sermons/.
    for i, evil in enumerate(
        (
            "../../../wp-config.php",
            "..%2F..%2Fevil.mp3",
            "/etc/passwd",
            "sub/dir/../x.php.mp3",
            ".htaccess",
        )
    ):
        r = bridge_post_audio(
            "georg",
            editor_pw,
            key=f"georg:e2e{RUN}:trav{i}:website",
            path=audio.path,
            filename=evil,
        )
        body = r.json()
        stored = str(body.get("file", ""))
        check(
            r.status_code == 201 and SAFE_NAME.fullmatch(stored) is not None,
            f"traversal name {evil!r} stored as {stored}",
            (r.status_code, body),
        )
    check(
        compose(
            "exec", "-T", "wordpress", "sh", "-c", "find wp-content/uploads -name '*.php' | wc -l"
        ).strip()
        == "0",
        "no .php files in uploads",
    )

    # Oversized uploads get a clear error rather than a confusing one.
    big = mp3(tmp / "big.mp3", 240, bitrate="320k")  # ~9.6 MB > upload_max_filesize 8M
    r = bridge_post_audio(
        "georg", editor_pw, key=f"georg:e2e{RUN}:big:website", path=big, filename="big.mp3"
    )
    check(
        r.status_code == 413 and "upload_max_filesize" in r.text,
        "upload_max_filesize exceeded is 413",
        (r.status_code, r.text[:300]),
    )
    huge = mp3(tmp / "huge.mp3", 340, bitrate="320k")  # ~13.6 MB > post_max_size 12M
    r = bridge_post_audio(
        "georg", editor_pw, key=f"georg:e2e{RUN}:huge:website", path=huge, filename="huge.mp3"
    )
    check(
        r.status_code == 413 and "post_max_size" in r.text,
        "post_max_size exceeded is 413",
        (r.status_code, r.text[:300]),
    )
    clear_debug_log()  # PHP itself logs a startup warning when post_max_size is exceeded.

    # Publish status, then front-end and admin pages render without PHP notices.
    out_pub = output("publish")
    pubkey = f"georg:e2e{RUN}:sermon-3:website"
    pub = out_pub.publish(request(pubkey, audio, "Published One", "Public words."))
    pstate = sermon_state(int(pub.external_id))
    check(pstate["status"] == "publish", "status=publish publishes", pstate["status"])
    r = httpx.get(pub.url or "", follow_redirects=True)
    check(
        r.status_code == 200 and "Public words." in r.text,
        "published sermon page renders",
        r.status_code,
    )
    r = httpx.get(pstate["audio"])
    check(
        r.status_code == 200 and r.headers.get("content-type") == "audio/mpeg",
        "audio URL serves audio/mpeg",
        r.headers.get("content-type"),
    )
    page_loads()

    log = debug_log()
    check(log.strip() == "", "debug.log has no PHP warnings/notices", log[-2000:])
    print(f"\n{len(failures)} failure(s)" + (": " + "; ".join(failures) if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
