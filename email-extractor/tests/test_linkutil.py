"""The /sklad/<key> derivation shared by httpapi.py (in-request) and orders/report.py
(the order worker's background thread, no Flask request) — #139.

The two callers MUST derive the identical key from the identical secret, or the worker's
Odoo message would hand out a link the dashboard's own /sklad/<key> route rejects.
"""
from app import httpapi, linkutil


def test_sklad_key_is_stable_for_the_same_secret():
    assert linkutil.sklad_key("s3cret") == linkutil.sklad_key("s3cret")


def test_sklad_key_differs_per_secret():
    assert linkutil.sklad_key("a") != linkutil.sklad_key("b")


def test_persistent_secret_survives_across_calls(tmp_path):
    first = linkutil.persistent_secret(tmp_path)
    second = linkutil.persistent_secret(tmp_path)
    assert first == second


def test_persistent_secret_differs_per_data_dir(tmp_path):
    a = linkutil.persistent_secret(tmp_path / "a")
    b = linkutil.persistent_secret(tmp_path / "b")
    assert a != b


def test_resolve_secret_prefers_configured_secret_key(tmp_path):
    class Cfg:
        secret_key = "configured"
        data_dir = str(tmp_path)
    assert linkutil.resolve_secret(Cfg()) == b"configured"


def test_resolve_secret_falls_back_to_persistent_secret(tmp_path):
    class Cfg:
        secret_key = ""
        data_dir = str(tmp_path)
    assert linkutil.resolve_secret(Cfg()) == linkutil.persistent_secret(tmp_path)


def test_sklad_url_is_empty_without_dashboard_base_url(tmp_path):
    class Cfg:
        dashboard_base_url = ""
        secret_key = "s"
        data_dir = str(tmp_path)
    assert linkutil.sklad_url(Cfg()) == ""


def test_sklad_url_strips_trailing_slash_and_signs_the_key(tmp_path):
    class Cfg:
        dashboard_base_url = "http://46.224.130.35:8099/"
        secret_key = "s3cret"
        data_dir = str(tmp_path)
    url = linkutil.sklad_url(Cfg())
    assert url == f"http://46.224.130.35:8099/sklad/{linkutil.sklad_key('s3cret')}"


# --- cross-module consistency: httpapi's dashboard link and the worker's link agree -----

def test_httpapi_sklad_key_is_the_same_function_as_linkutil():
    """A regression guard against the two derivations drifting apart again (#139's whole
    point) — httpapi.py must not keep its own copy."""
    assert httpapi.sklad_key is linkutil.sklad_key
