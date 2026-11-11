import os
import stat
import time

import pytest

from comapeo_qgis.core.media import (
    FetchOutcome,
    MediaCache,
    MediaState,
    classify_status,
    is_media_url,
)
from comapeo_qgis.core.model import Attachment, attachments_from_json, parse_attachments

PHOTO = Attachment(
    type="photo",
    name="IMG_0001.JPG",
    key="drive/abc/photo/IMG_0001.JPG",
    urls={
        "thumbnail": "http://127.0.0.1:5001/t",
        "preview": "http://127.0.0.1:5001/p",
        "original": "http://127.0.0.1:5001/o",
    },
)
AUDIO = Attachment(
    type="audio", name="note", key="drive/abc/audio/n", urls={"original": "http://x"}
)


def test_paths_depend_on_project_key_and_variant_not_url(tmp_path):
    cache = MediaCache(tmp_path)
    same_key_new_port = Attachment(
        "photo", "IMG_0001.JPG", PHOTO.key, {"thumbnail": "http://127.0.0.1:9/t"}
    )
    a = cache.path_for("p1", PHOTO, "thumbnail")
    assert a == cache.path_for("p1", same_key_new_port, "thumbnail")
    assert a != cache.path_for("p2", PHOTO, "thumbnail")
    assert a != cache.path_for("p1", PHOTO, "preview")
    other = Attachment("photo", "IMG_0001.JPG", "drive/other", PHOTO.urls)
    assert a != cache.path_for("p1", other, "thumbnail")
    assert a.is_relative_to(tmp_path)
    assert a.name == "thumbnail.jpg"


def test_hostile_keys_stay_inside_the_cache(tmp_path):
    cache = MediaCache(tmp_path)
    evil = Attachment("photo", "../../x.sh", "../../../etc/passwd", {})
    path = cache.path_for("../..", evil, "original")
    assert path.resolve().is_relative_to(tmp_path.resolve())
    assert path.name == "original.jpg"


@pytest.mark.parametrize(
    ("attachment_type", "name", "expected"),
    [
        ("photo", "x.bat", "original.jpg"),
        ("photo", "x.jpg.exe", "original.jpg"),
        ("video", "x.bat", "original.mp4"),
        ("video", "clip.html", "original.mp4"),
        ("audio", "voice.command", "original.m4a"),
        ("audio", "voice.lnk", "original.m4a"),
        ("photo", "IMG_1.PNG", "original.png"),
        ("photo", "IMG_1.jpeg", "original.jpeg"),
        ("audio", "note.MP3", "original.mp3"),
        ("video", "clip.MOV", "original.mov"),
        ("video", "clip.mp3", "original.mp4"),
        ("document", "report.pdf", "original.bin"),
        ("", "x.exe", "original.bin"),
    ],
)
def test_file_extension_never_comes_from_outside_a_media_allow_list(
    tmp_path, attachment_type, name, expected
):
    attachment = Attachment(attachment_type, name, "k", {})
    path = MediaCache(tmp_path).store("p", attachment, "original", b"data")
    assert path.name == expected
    assert path.read_bytes() == b"data"


def test_keys_that_are_not_valid_unicode_still_get_a_path(tmp_path):
    cache = MediaCache(tmp_path)
    odd = Attachment("photo", "a.jpg", "drive/\udcff/photo/a", {})
    path = cache.store("p\ud800", odd, "thumbnail", b"x")
    assert path.resolve().is_relative_to(tmp_path.resolve())
    assert cache.lookup("p\ud800", odd, "thumbnail").state is MediaState.CACHED


def test_empty_200_body_is_not_cached(tmp_path):
    cache = MediaCache(tmp_path)
    assert cache.record_fetch("p1", PHOTO, "thumbnail", 200, b"").state is (
        MediaState.NOT_FETCHED
    )
    assert cache.record_fetch("p1", PHOTO, "thumbnail", 200).state is (
        MediaState.NOT_FETCHED
    )
    assert not cache.path_for("p1", PHOTO, "thumbnail").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_cache_folders_and_files_are_private(tmp_path):
    root = tmp_path / "profile" / "media"
    path = MediaCache(root).store("p1", PHOTO, "thumbnail", b"x")
    for item in (root, path.parent.parent, path.parent):
        assert stat.S_IMODE(item.stat().st_mode) == 0o700, item
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_partial_files_from_an_interrupted_write_are_swept(tmp_path):
    cache = MediaCache(tmp_path)
    kept = cache.store("p1", PHOTO, "thumbnail", b"x")
    old = kept.parent / ".part-old"
    fresh = kept.parent / ".part-fresh"
    old.write_bytes(b"half")
    fresh.write_bytes(b"half")
    an_hour_ago = time.time() - 7200
    os.utime(old, (an_hour_ago, an_hour_ago))
    assert MediaCache(tmp_path).remove_partial_files() == 1
    assert not old.exists() and fresh.exists() and kept.exists()
    assert MediaCache(tmp_path / "missing").remove_partial_files() == 0


def test_extension_falls_back_to_the_attachment_type(tmp_path):
    cache = MediaCache(tmp_path)
    assert cache.path_for("p", AUDIO, "original").name == "original.m4a"
    weird = Attachment("video", "clip.$$$", "k", {})
    assert cache.path_for("p", weird, "original").name == "original.mp4"


def test_unknown_variant_is_an_error(tmp_path):
    with pytest.raises(ValueError):
        MediaCache(tmp_path).path_for("p", PHOTO, "huge")


def test_fetch_store_and_lookup(tmp_path):
    cache = MediaCache(tmp_path)
    entry = cache.lookup("p1", PHOTO, "thumbnail")
    assert entry.state is MediaState.NOT_FETCHED
    assert entry.url == "http://127.0.0.1:5001/t"
    entry = cache.record_fetch("p1", PHOTO, "thumbnail", 200, b"jpeg bytes")
    assert entry.state is MediaState.CACHED
    assert entry.path.read_bytes() == b"jpeg bytes"
    assert MediaCache(tmp_path).lookup("p1", PHOTO, "thumbnail").state is (
        MediaState.CACHED
    )


def test_404_is_not_yet_synced_until_refresh(tmp_path):
    cache = MediaCache(tmp_path)
    entry = cache.record_fetch("p1", PHOTO, "preview", 404)
    assert entry.state is MediaState.NOT_SYNCED
    assert cache.lookup("p1", PHOTO, "preview").state is MediaState.NOT_SYNCED
    assert cache.lookup("p1", PHOTO, "thumbnail").state is MediaState.NOT_FETCHED
    cache.retry_not_synced()
    assert cache.lookup("p1", PHOTO, "preview").state is MediaState.NOT_FETCHED
    cache.record_fetch("p1", PHOTO, "preview", 404)
    assert cache.record_fetch("p1", PHOTO, "preview", 200, b"x").state is (
        MediaState.CACHED
    )


def test_server_error_is_not_marked_not_synced(tmp_path):
    cache = MediaCache(tmp_path)
    assert cache.record_fetch("p1", PHOTO, "preview", 500).state is (
        MediaState.NOT_FETCHED
    )


def test_variant_without_url_is_unavailable(tmp_path):
    entry = MediaCache(tmp_path).lookup("p1", AUDIO, "thumbnail")
    assert entry.state is MediaState.UNAVAILABLE and entry.url is None


def test_remove_project(tmp_path):
    cache = MediaCache(tmp_path)
    cache.store("p1", PHOTO, "thumbnail", b"1")
    cache.store("p2", PHOTO, "thumbnail", b"2")
    cache.remove_project("p1")
    assert cache.lookup("p1", PHOTO, "thumbnail").state is MediaState.NOT_FETCHED
    assert cache.lookup("p2", PHOTO, "thumbnail").state is MediaState.CACHED


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, FetchOutcome.OK),
        (206, FetchOutcome.ERROR),
        (404, FetchOutcome.NOT_SYNCED),
        (500, FetchOutcome.ERROR),
        (403, FetchOutcome.ERROR),
    ],
)
def test_classify_status(status, outcome):
    assert classify_status(status) is outcome


@pytest.mark.parametrize(
    ("attachment_type", "content_type", "expected"),
    [
        ("audio", "audio/wav", "original.wav"),
        ("audio", "audio/x-wav", "original.wav"),
        ("audio", "audio/mp4", "original.m4a"),
        ("audio", "audio/mpeg", "original.mp3"),
        ("photo", "image/jpeg", "original.jpg"),
        ("photo", "IMAGE/PNG; charset=binary", "original.png"),
        ("video", "video/quicktime", "original.mov"),
        ("photo", "text/html", "original.jpg"),
        ("photo", "application/x-msdownload", "original.jpg"),
        ("photo", "audio/wav", "original.jpg"),
        ("audio", "image/png", "original.m4a"),
        ("audio", None, "original.m4a"),
        ("document", "application/pdf", "original.bin"),
    ],
)
def test_extension_follows_the_content_type_within_the_allow_list(
    tmp_path, attachment_type, content_type, expected
):
    attachment = Attachment(attachment_type, "1f0e9d8c7b6a", "k", {})
    cache = MediaCache(tmp_path)
    path = cache.store("p", attachment, "original", b"data", content_type)
    assert path.name == expected
    assert cache.lookup("p", attachment, "original").path == path


def test_content_type_wins_over_the_name(tmp_path):
    attachment = Attachment("audio", "note.mp3", "k", {})
    path = MediaCache(tmp_path).store("p", attachment, "original", b"x", "audio/wav")
    assert path.name == "original.wav"


def test_a_file_saved_under_another_extension_is_found_and_replaced(tmp_path):
    cache = MediaCache(tmp_path)
    wav = cache.store("p", AUDIO, "original", b"old", "audio/wav")
    entry = MediaCache(tmp_path).lookup("p", AUDIO, "original")
    assert entry.state is MediaState.CACHED and entry.path == wav
    m4a = cache.store("p", AUDIO, "original", b"new", "audio/mp4")
    assert m4a.name == "original.m4a" and not wav.exists()
    assert cache.lookup("p", AUDIO, "original").path.read_bytes() == b"new"


def test_streamed_download_commits_into_place(tmp_path):
    cache = MediaCache(tmp_path)
    cache.record_fetch("p", PHOTO, "original", 404)
    partial = cache.partial_file("p", PHOTO, "original")
    with open(partial, "ab") as f:
        f.write(b"part one ")
        f.write(b"part two")
    assert cache.lookup("p", PHOTO, "original").state is MediaState.NOT_SYNCED
    path = cache.commit(partial, "p", PHOTO, "original", "image/jpeg")
    assert not partial.exists()
    entry = cache.lookup("p", PHOTO, "original")
    assert entry.state is MediaState.CACHED and entry.path == path
    assert path.read_bytes() == b"part one part two"


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("http://127.0.0.1:5001/blobs/a/b", True),
        ("http://localhost:5001/x", True),
        ("http://[::1]:5001/x", True),
        ("https://127.0.0.1:5001/x", False),
        ("http://example.org/x", False),
        ("http://127.0.0.1.example.org/x", False),
        ("file:///etc/passwd", False),
        ("http://[::1/x", False),
        ("", False),
        (None, False),
    ],
)
def test_only_loopback_http_urls_are_media_urls(url, ok):
    assert is_media_url(url) is ok


def test_attachments_read_back_from_the_cache_column():
    text = (
        '[{"type": "photo", "name": "a", "key": "k1", "urls": {"thumbnail": "u"}},'
        ' {"type": "audio", "key": ""}, "junk", {"type": "audio", "key": "k2"}]'
    )
    found = attachments_from_json(text)
    assert [a.key for a in found] == ["k1", "k2"]
    assert found[0].urls == {"thumbnail": "u"}
    assert attachments_from_json("not json") == ()
    assert attachments_from_json(None) == ()


def test_attachments_keep_only_the_known_media_variants():
    [attachment] = parse_attachments(
        [
            {
                "type": "photo",
                "key": "k1",
                "urls": {
                    "thumbnail": "http://127.0.0.1:1/t",
                    "huge": "http://127.0.0.1:1/h",
                    "original": "http://127.0.0.1:1/o",
                },
            }
        ]
    )
    assert dict(attachment.urls) == {
        "thumbnail": "http://127.0.0.1:1/t",
        "original": "http://127.0.0.1:1/o",
    }


def test_committing_an_unknown_variant_leaves_no_partial_file(tmp_path):
    cache = MediaCache(tmp_path)
    partial = cache.partial_file("p", PHOTO, "original")
    partial.write_bytes(b"data")
    with pytest.raises(ValueError):
        cache.commit(partial, "p", PHOTO, "huge", "image/jpeg")
    assert not partial.exists()
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
