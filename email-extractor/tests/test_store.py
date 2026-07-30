"""Storage-directory identity (#21).

The bug: safe_id() truncated the Message-ID to 120 sanitized chars, so two distinct
emails whose IDs share a 120-char prefix (long auto-generated IDs from one mailer)
got distinct `messages` rows but the SAME directory — the second email's raw.eml and
attachments overwrote the first's, and /files//eml then served the WRONG originals
to n8n (AI-Vision / forward on someone else's attachment), with no error.
"""
from pathlib import Path

from app import store

PREFIX = "x" * 130          # 130 identical chars: collides under a 120-char cut


def _id(tag: str) -> str:
    return f"<{PREFIX}.{tag}@mailer.example>"


def test_ids_sharing_a_120_char_prefix_get_different_dirs():
    a, b = store.safe_id(_id("a")), store.safe_id(_id("b"))
    assert a != b, "distinct Message-IDs must never map to the same storage dir"


def test_dir_name_stays_filesystem_safe_and_bounded():
    sid = store.safe_id("<a b/c\\d..%$#@mail>")
    assert "/" not in sid and "\\" not in sid and " " not in sid
    assert 0 < len(sid) <= 160
    assert store.safe_id("") == store.safe_id(""), "stable for a missing id"
    assert store.safe_id("") != ""


def test_same_id_maps_to_the_same_dir_every_time():
    assert store.safe_id(_id("a")) == store.safe_id(_id("a"))


def test_colliding_emails_do_not_overwrite_each_others_files(tmp_path):
    raw_a, raw_b = b"EML-A", b"EML-B"
    ap, afiles = store.save_message(str(tmp_path), _id("a"), raw_a,
                                    [{"filename": "faktura.pdf", "_data": b"PDF-A"}],
                                    "http://email-extractor:8099", "tok")
    bp, bfiles = store.save_message(str(tmp_path), _id("b"), raw_b,
                                    [{"filename": "faktura.pdf", "_data": b"PDF-B"}],
                                    "http://email-extractor:8099", "tok")
    assert Path(ap).read_bytes() == raw_a, "the first email's raw.eml was overwritten"
    assert Path(bp).read_bytes() == raw_b
    assert Path(afiles[0]["path"]).read_bytes() == b"PDF-A"
    assert Path(bfiles[0]["path"]).read_bytes() == b"PDF-B"
    assert Path(ap).parent != Path(bp).parent


def test_legacy_dirs_written_by_the_old_scheme_stay_readable(tmp_path):
    """Live data already on the add-on volume uses the old 120-char dir name."""
    mid = _id("legacy")
    old = tmp_path / store.legacy_safe_id(mid)
    old.mkdir()
    (old / "raw.eml").write_bytes(b"OLD-EML")
    (old / "att0__stary.pdf").write_bytes(b"OLD-PDF")
    d = store.message_dir(str(tmp_path), mid)
    assert d == old, "an existing legacy dir must still be found"
    assert (d / "raw.eml").read_bytes() == b"OLD-EML"


def test_new_writes_prefer_the_new_scheme(tmp_path):
    mid = _id("fresh")
    raw_path, _ = store.save_message(str(tmp_path), mid, b"NEW", [],
                                     "http://email-extractor:8099", "")
    assert Path(raw_path).parent.name == store.safe_id(mid)
    assert Path(raw_path).parent.name != store.legacy_safe_id(mid)


def test_a_crafted_traversal_segment_cannot_escape_the_store(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "raw.eml").write_bytes(b"SECRET")
    d = store.message_dir(str(tmp_path), "../outside")
    assert not (d / "raw.eml").exists(), "must not reach a dir above the store"


def test_dir_name_from_a_stored_url_resolves_back(tmp_path):
    """file_url carries the dir name, so /files/<dirname>/<idx> must resolve too."""
    mid = "<roundtrip@m.example>"
    store.save_message(str(tmp_path), mid, b"EML", [{"filename": "a.pdf", "_data": b"P"}],
                       "http://x", "")
    assert store.message_dir(str(tmp_path), store.safe_id(mid)).name == store.safe_id(mid)
    assert (store.message_dir(str(tmp_path), store.safe_id(mid)) / "raw.eml").exists()


# ---- #22: the API token must never be baked into a stored URL ----

def test_file_url_carries_no_token(tmp_path):
    _, files = store.save_message(str(tmp_path), "<t@m>", b"EML",
                                  [{"filename": "a.pdf", "_data": b"P"}],
                                  "http://email-extractor:8099")
    assert "token" not in files[0]["url"], "the secret must not be persisted in Postgres"
    assert files[0]["url"].startswith("http://email-extractor:8099/files/")
    assert files[0]["url"].endswith("/0")


def test_file_url_uses_the_configured_base(tmp_path):
    _, files = store.save_message(str(tmp_path), "<t2@m>", b"E",
                                  [{"filename": "a.pdf", "_data": b"P"}],
                                  "http://e0ac7775-email-extractor:8099")
    assert "localhost" not in files[0]["url"]
    assert files[0]["url"].startswith("http://e0ac7775-email-extractor:8099/files/")
