"""
geobridge.modules.cds_download
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

CDS API download path for datasets not available as ARCO Zarr.

Implements the OGC API Processes pattern used by the new CDS API:

1. POST a retrieval request to ``/processes/{dataset}/execution``
2. Poll the job status URL until the job reaches ``successful`` or ``failed``
3. Download the result file (NetCDF or GRIB)
4. Open with xarray, subset if needed, write as Cloud Optimized GeoTIFF

This module complements :mod:`geobridge.modules.extract` which covers the
ARCO Zarr path.  The two modules share the same output contract — both
produce a GeoTIFF at a user-specified path — so downstream code (the QGIS
plugin, the Athens demo) can treat them interchangeably.

Authentication
--------------
Uses the same bearer token as the ARCO path::

    Authorization: Bearer <CDS_API_KEY>

Rate limiting and queue times
------------------------------
CDS API jobs are queued on ECMWF's servers.  Queue times vary from
seconds (small requests, uncongested queue) to hours (large global
requests during peak hours).  The :func:`cds_to_geotiff` function polls
at increasing intervals and respects a configurable timeout.

Usage
-----
::

    import geobridge as gb
    gb.authenticate()

    tif = gb.cds_to_geotiff(
        dataset="reanalysis-era5-pressure-levels",
        request={
            "product_type": ["reanalysis"],
            "variable": ["temperature", "geopotential"],
            "pressure_level": ["500", "850"],
            "year": ["2023"],
            "month": ["07"],
            "day": ["01", "15"],
            "time": ["12:00"],
            "data_format": "netcdf",
        },
        bbox=(23.0, 37.5, 24.5, 38.5),
        output_path="era5_pl_500hpa.tif",
    )
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CDS_API_BASE = "https://cds.climate.copernicus.eu/api/retrieve/v1"

# Polling strategy: start at 5s, double up to 60s max
_POLL_INITIAL = 5.0
_POLL_MAX = 60.0
_POLL_BACKOFF = 2.0


class CdsApiError(Exception):
    """Raised when the CDS API returns an error."""


class CdsJobTimeout(Exception):
    """Raised when a CDS job does not complete within the timeout."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _auth_headers() -> dict:
    from geobridge.auth import auth_header
    return {**auth_header(), "Content-Type": "application/json",
            "Accept": "application/json"}


def _post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=_auth_headers(),
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _download_file(url: str, dest: Path, timeout: int = 300) -> Path:
    req = urllib.request.Request(url, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dest.write_bytes(resp.read())
    return dest


# ---------------------------------------------------------------------------
# Job submission and polling
# ---------------------------------------------------------------------------

def _submit_job(dataset_id: str, request: dict) -> str:
    """Submit a retrieval job and return the job status URL."""
    cds_id = dataset_id.replace("_", "-")
    url = f"{CDS_API_BASE}/processes/{cds_id}/execution"

    # Wrap in OGC API Processes format
    payload = {"inputs": request}

    try:
        resp = _post_json(url, payload)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise CdsApiError(
            f"CDS API rejected the request for '{cds_id}' "
            f"(HTTP {exc.code}):\n{body}\n\n"
            "Common causes:\n"
            "  • Invalid parameter combination — check constraints\n"
            "  • Dataset licence not accepted on CDS portal\n"
            "  • Dataset not available via this API endpoint"
        ) from exc

    job_id = resp.get("jobID") or resp.get("id")
    if not job_id:
        raise CdsApiError(f"No job ID in response: {resp}")

    # Job status URL
    status_url = f"{CDS_API_BASE}/jobs/{job_id}"
    logger.info("CDS job submitted: %s", job_id)
    return status_url


def _poll_job(
    status_url: str,
    timeout_seconds: float = 3600,
    progress_callback=None,
) -> str:
    """Poll the job until it succeeds or fails.  Returns the download URL."""
    interval = _POLL_INITIAL
    elapsed = 0.0
    start = time.monotonic()

    while True:
        try:
            status = _get_json(status_url)
        except Exception as exc:
            logger.warning("Poll failed: %s — retrying", exc)
            time.sleep(interval)
            continue

        job_status = status.get("status", "")
        message = status.get("message", "")

        if progress_callback:
            progress_callback(f"CDS job status: {job_status} — {message}")

        if job_status in ("successful", "finished"):
            # Find the download URL
            for link in status.get("links", []):
                if link.get("rel") in ("result", "download"):
                    return link["href"]
            # Fallback: results endpoint
            return status_url.replace("/jobs/", "/jobs/") + "/results"

        if job_status in ("failed", "dismissed", "error"):
            detail = status.get("detail") or status.get("message") or str(status)
            raise CdsApiError(
                f"CDS job failed:\n{detail}\n\n"
                "If the error mentions 'licence', visit the dataset page on "
                "https://cds.climate.copernicus.eu and accept the terms of use."
            )

        elapsed = time.monotonic() - start
        if elapsed > timeout_seconds:
            raise CdsJobTimeout(
                f"CDS job did not complete within {timeout_seconds/60:.0f} minutes.\n"
                f"Status URL: {status_url}\n"
                "The job is still running on the CDS server — you can check "
                "its status at https://cds.climate.copernicus.eu/requests"
            )

        logger.debug("CDS job %s after %.0fs — waiting %.0fs", job_status, elapsed, interval)
        time.sleep(interval)
        interval = min(interval * _POLL_BACKOFF, _POLL_MAX)


# ---------------------------------------------------------------------------
# NetCDF → GeoTIFF conversion
# ---------------------------------------------------------------------------

def _netcdf_to_geotiff(
    nc_path: Path,
    variable: str,
    bbox: Optional[tuple[float, float, float, float]],
    output_path: Path,
    cog: bool = True,
) -> Path:
    """Open a NetCDF file, optionally subset, write a GeoTIFF."""
    try:
        import xarray as xr
        import rioxarray  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "xarray and rioxarray are required for NetCDF conversion.\n"
            "Install with: pip install 'geobridge[zarr]'"
        ) from exc

    ds = xr.open_dataset(nc_path, engine="netcdf4")

    # Find the variable — try the requested name, then the first data variable
    if variable in ds:
        da = ds[variable]
    else:
        data_vars = [v for v in ds.data_vars]
        if not data_vars:
            raise ValueError(f"No data variables found in {nc_path}")
        logger.warning(
            "Variable '%s' not in file. Using '%s'. Available: %s",
            variable, data_vars[0], data_vars,
        )
        da = ds[data_vars[0]]

    # Detect and rename spatial dimensions
    lat_names = [d for d in da.dims if "lat" in d.lower()]
    lon_names = [d for d in da.dims if "lon" in d.lower()]
    if lat_names and lat_names[0] != "y":
        da = da.rename({lat_names[0]: "y", lon_names[0]: "x"})

    # Spatial subset
    if bbox:
        west, south, east, north = bbox
        da = da.sel(
            y=slice(north, south) if da.y[0] > da.y[-1] else slice(south, north),
            x=slice(west, east),
        )

    # If there are remaining non-spatial dimensions (time, level), take mean
    extra_dims = [d for d in da.dims if d not in ("x", "y")]
    if extra_dims:
        logger.info("Collapsing extra dims %s by mean", extra_dims)
        da = da.mean(dim=extra_dims)

    da = da.compute()

    # Write GeoTIFF
    da.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=True)
    da.rio.write_crs("EPSG:4326", inplace=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if cog:
        tmp = output_path.with_suffix(".tmp.tif")
        da.rio.to_raster(str(tmp))
        try:
            import subprocess
            result = subprocess.run(
                ["gdal_translate", "-of", "COG", str(tmp), str(output_path)],
                capture_output=True
            )
            tmp.unlink(missing_ok=True)
            if result.returncode != 0:
                # Fallback — just move the regular tif
                tmp.rename(output_path)
        except FileNotFoundError:
            tmp.rename(output_path)
    else:
        da.rio.to_raster(str(output_path))

    return output_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def cds_to_geotiff(
    dataset: str,
    request: dict,
    variable: Optional[str] = None,
    bbox: Optional[tuple[float, float, float, float]] = None,
    output_path: Optional[str] = None,
    timeout: float = 3600,
    progress_callback=None,
    cog: bool = True,
) -> Path:
    """Download a dataset from the CDS API and convert to GeoTIFF.

    This is the fallback extraction path for datasets not available as
    ARCO Zarr.  The function submits a CDS API job, polls until it
    completes, downloads the result, and converts it to a Cloud Optimized
    GeoTIFF.

    Parameters
    ----------
    dataset : str
        CDS dataset identifier (hyphens or underscores).
    request : dict
        CDS API request parameters — the same dict you would pass to
        ``cdsapi.Client().retrieve(dataset, request, ...)``.
        Must include at minimum ``variable`` and ``data_format``.
    variable : str, optional
        Variable name to extract from the downloaded file.  If None,
        uses the first variable in the ``request`` dict.
    bbox : tuple, optional
        ``(west, south, east, north)`` for spatial subset of the downloaded
        file.  Applied after download.
    output_path : str, optional
        Path for the output GeoTIFF.  Defaults to a temp file.
    timeout : float
        Maximum polling time in seconds (default 3600 = 1 hour).
    progress_callback : callable, optional
        Called with a status string at each polling step.
        Signature: ``callback(message: str)``.
    cog : bool
        Write a Cloud Optimized GeoTIFF (default True).

    Returns
    -------
    Path
        Path to the output GeoTIFF.

    Raises
    ------
    CdsApiError
        When the CDS API rejects the request or the job fails.
    CdsJobTimeout
        When the job does not complete within *timeout* seconds.

    Examples
    --------
    >>> tif = gb.cds_to_geotiff(
    ...     dataset="reanalysis-era5-pressure-levels",
    ...     request={
    ...         "product_type": ["reanalysis"],
    ...         "variable": ["temperature"],
    ...         "pressure_level": ["500"],
    ...         "year": ["2023"], "month": ["07"], "day": ["15"],
    ...         "time": ["12:00"],
    ...         "data_format": "netcdf",
    ...     },
    ...     bbox=(23.0, 37.5, 24.5, 38.5),
    ...     output_path="era5_500hpa_temp.tif",
    ... )
    """
    import tempfile

    # Ensure data_format is set
    if "data_format" not in request:
        request = dict(request)
        request["data_format"] = "netcdf"

    # Infer variable from request if not given
    if variable is None:
        vars_in_request = request.get("variable", [])
        if isinstance(vars_in_request, list) and vars_in_request:
            variable = vars_in_request[0]
        elif isinstance(vars_in_request, str):
            variable = vars_in_request

    if progress_callback:
        progress_callback(f"Submitting CDS request for {dataset}…")

    status_url = _submit_job(dataset, request)

    if progress_callback:
        progress_callback("Job queued — waiting for CDS server…")

    download_url = _poll_job(status_url, timeout_seconds=timeout,
                             progress_callback=progress_callback)

    if progress_callback:
        progress_callback("Downloading result file…")

    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as f:
        nc_path = Path(f.name)

    try:
        _download_file(download_url, nc_path)
    except Exception as exc:
        nc_path.unlink(missing_ok=True)
        raise CdsApiError(f"Download failed: {exc}") from exc

    if progress_callback:
        progress_callback("Converting to GeoTIFF…")

    if output_path is None:
        output_path = nc_path.with_suffix(".tif")
    else:
        output_path = Path(output_path)

    try:
        result = _netcdf_to_geotiff(
            nc_path, variable or "", bbox, output_path, cog=cog
        )
    finally:
        nc_path.unlink(missing_ok=True)

    if progress_callback:
        progress_callback(f"Done: {result.name}")

    return result
