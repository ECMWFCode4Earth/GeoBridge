"""
geobridge.auth
~~~~~~~~~~~~~~

Authentication for Copernicus Data Stores access.


Credentials are resolved in priority order:
    1. Explicit ``key=`` argument to :func:`authenticate`
    2. Environment variable ``CDS_API_KEY``
    3. ``~/.cdsapirc`` (standard CDS client configuration file)

References
----------
- ECMWF Knowledge Base: Analysis Ready Cloud Optimised (ARCO) Data
  https://confluence.ecmwf.int/x/8aYZJg
- CDS API setup: https://cds.climate.copernicus.eu/how-to-api
"""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_API_URL = "https://cds.climate.copernicus.eu/api"
_CONFIG_FILE = pathlib.Path.home() / ".cdsapirc"

# Module-level singleton — one session per Python process
_session: Optional["_AuthSession"] = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AuthenticationError(RuntimeError):
    """Raised when credentials are missing or improperly formatted."""


# ---------------------------------------------------------------------------
# Internal session object
# ---------------------------------------------------------------------------

class _AuthSession:
    """Holds the API key for the current Python process.

    The CDS API key is used directly as the bearer token in HTTP
    Authorization headers when accessing ARCO Zarr resources.
    """

    def __init__(self, api_key: str, api_url: str) -> None:
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")

    def get_token(self) -> str:
        """Return the bearer token (the CDS API key itself)."""
        return self.api_key

    @property
    def auth_header(self) -> dict[str, str]:
        """Return Authorization header dict for HTTP requests to ARCO."""
        return {"Authorization": f"Bearer {self.api_key}"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def authenticate(
    key: Optional[str] = None,
    url: str = _DEFAULT_API_URL,
) -> None:
    """Initialise a CDS API session.

    Call this once before using any other geobridge function. Credentials
    are looked up in priority order:

        1. The ``key`` argument (explicit)
        2. The ``CDS_API_KEY`` environment variable
        3. ``~/.cdsapirc`` (``key: <your-key>`` on one line)

    Parameters
    ----------
    key : str, optional
        Your CDS personal access token. Get one at
        https://cds.climate.copernicus.eu/profile.
    url : str
        CDS API base URL. Rarely needs changing.

    Raises
    ------
    AuthenticationError
        If no API key can be found in any source.

    Examples
    --------
    >>> import geobridge as gb
    >>> gb.authenticate()                       # uses ~/.cdsapirc
    >>> gb.authenticate(key="xxxxxxxx-xxxx")    # explicit key
    """
    global _session
    resolved_key = _resolve_key(key)
    _session = _AuthSession(api_key=resolved_key, api_url=url)
    logger.info("geobridge: authenticated (key ending …%s)", resolved_key[-6:])


def get_token() -> str:
    """Return the bearer token for ARCO-Zarr access.

    Under the current Copernicus access model this is simply the CDS
    API key. The function is retained for forward-compatibility in case
    ECMWF ever reintroduces a token-exchange step.

    Raises
    ------
    AuthenticationError
        If :func:`authenticate` has not been called yet.
    """
    return _require_session().get_token()


def auth_header() -> dict[str, str]:
    """Return the HTTP Authorization header dict for ARCO requests.

    Useful as the ``storage_options["headers"]`` value when calling
    ``xarray.open_zarr``.

    Examples
    --------
    >>> import geobridge as gb, xarray as xr
    >>> gb.authenticate()
    >>> ds = xr.open_zarr(zarr_url, storage_options={"headers": gb.auth_header()})
    """
    return _require_session().auth_header


def is_authenticated() -> bool:
    """Return True if :func:`authenticate` has been called successfully."""
    return _session is not None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_session() -> _AuthSession:
    """Raise a helpful error if authenticate() has not been called."""
    if _session is None:
        raise AuthenticationError(
            "Not authenticated. Call geobridge.authenticate() first.\n\n"
            "Example:\n"
            "    import geobridge as gb\n"
            "    gb.authenticate()  # reads ~/.cdsapirc"
        )
    return _session


def _resolve_key(explicit_key: Optional[str]) -> str:
    """Find an API key from the three sources, in priority order."""
    if explicit_key:
        return explicit_key.strip()

    env_key = os.environ.get("CDS_API_KEY")
    if env_key:
        return env_key.strip()

    if _CONFIG_FILE.exists():
        for line in _CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("key"):
                separator = ":" if ":" in line else "="
                _, _, value = line.partition(separator)
                value = value.strip().strip('"').strip("'")
                if value:
                    return value

    raise AuthenticationError(
        "No CDS API key found. Provide one via:\n"
        "  1. gb.authenticate(key='your-key')\n"
        "  2. export CDS_API_KEY='your-key'\n"
        "  3. echo 'key: your-key' >> ~/.cdsapirc\n\n"
        "Get your key at: https://cds.climate.copernicus.eu/profile"
    )
