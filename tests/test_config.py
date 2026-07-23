"""Config validation.

The cleanup endpoint is user-editable in config.toml and receives the API key
as a bearer token, so the scheme is a security-relevant setting rather than a
cosmetic one.
"""

from flow.config import checked_base_url

DEFAULT = "https://openrouter.ai/api/v1"


def test_https_endpoint_is_kept():
    assert checked_base_url("https://api.example.com/v1", DEFAULT) == (
        "https://api.example.com/v1"
    )


def test_plain_http_endpoint_falls_back_to_the_default():
    assert checked_base_url("http://api.example.com/v1", DEFAULT) == DEFAULT


def test_local_http_endpoint_is_allowed():
    """A model server on this machine is the one case where plain HTTP leaks
    nothing — the request never touches the network."""
    for url in (
        "http://localhost:11434/v1",
        "http://127.0.0.1:1234/v1",
    ):
        assert checked_base_url(url, DEFAULT) == url


def test_missing_scheme_falls_back():
    assert checked_base_url("api.example.com/v1", DEFAULT) == DEFAULT


def test_non_http_scheme_falls_back():
    assert checked_base_url("ftp://api.example.com/v1", DEFAULT) == DEFAULT
