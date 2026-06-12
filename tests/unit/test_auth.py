"""Tests for geobridge.auth — credential resolution and session management."""

import os
import pathlib
import pytest

from geobridge import auth


@pytest.fixture(autouse=True)
def reset_session():
    """Reset module-level session state between tests."""
    auth._session = None
    yield
    auth._session = None


def test_authenticate_with_explicit_key():
    auth.authenticate(key="test-key-12345678")
    assert auth.is_authenticated()
    assert auth._session.api_key == "test-key-12345678"


def test_authenticate_with_env_var(monkeypatch):
    monkeypatch.setenv("CDS_API_KEY", "env-key-abcdefgh")
    auth.authenticate()
    assert auth._session.api_key == "env-key-abcdefgh"


def test_authenticate_explicit_key_beats_env(monkeypatch):
    monkeypatch.setenv("CDS_API_KEY", "env-key")
    auth.authenticate(key="explicit-key")
    assert auth._session.api_key == "explicit-key"


def test_authenticate_with_config_file(monkeypatch, tmp_path):
    config = tmp_path / ".cdsapirc"
    config.write_text("url: https://cds.climate.copernicus.eu/api/v2\nkey: file-key-1234\n")
    monkeypatch.setattr(auth, "_CONFIG_FILE", config)
    monkeypatch.delenv("CDS_API_KEY", raising=False)

    auth.authenticate()
    assert auth._session.api_key == "file-key-1234"


def test_authenticate_config_file_with_equals_sign(monkeypatch, tmp_path):
    config = tmp_path / ".cdsapirc"
    config.write_text("key=quoted-key\n")
    monkeypatch.setattr(auth, "_CONFIG_FILE", config)
    monkeypatch.delenv("CDS_API_KEY", raising=False)

    auth.authenticate()
    assert auth._session.api_key == "quoted-key"


def test_authenticate_strips_quotes_in_config(monkeypatch, tmp_path):
    config = tmp_path / ".cdsapirc"
    config.write_text('key: "double-quoted-key"\n')
    monkeypatch.setattr(auth, "_CONFIG_FILE", config)
    monkeypatch.delenv("CDS_API_KEY", raising=False)

    auth.authenticate()
    assert auth._session.api_key == "double-quoted-key"


def test_authenticate_no_credentials_raises(monkeypatch, tmp_path):
    nonexistent = tmp_path / ".cdsapirc"
    monkeypatch.setattr(auth, "_CONFIG_FILE", nonexistent)
    monkeypatch.delenv("CDS_API_KEY", raising=False)

    with pytest.raises(auth.AuthenticationError) as exc_info:
        auth.authenticate()
    assert "No CDS API key found" in str(exc_info.value)


def test_get_token_without_authenticate_raises():
    with pytest.raises(auth.AuthenticationError) as exc_info:
        auth.get_token()
    assert "Not authenticated" in str(exc_info.value)


def test_is_authenticated_initially_false():
    assert auth.is_authenticated() is False


def test_is_authenticated_after_authenticate():
    auth.authenticate(key="any-key")
    assert auth.is_authenticated() is True
