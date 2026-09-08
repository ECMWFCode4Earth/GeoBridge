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
The job-submission API requires the CDS key as a ``PRIVATE-TOKEN`` header
(unlike the ARCO-Zarr path, which uses ``Authorization: Bearer``)::

    PRIVATE-TOKEN: <CDS_API_KEY>

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
import re
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
_POLL_BACKOFF = 15.0


class CdsApiError(Exception):
    """Raised when the CDS API returns an error."""


class CdsJobTimeout(Exception):
    """Raised when a CDS job does not complete within the timeout."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _auth_headers() -> dict:
    from geobridge.auth import get_token
    return {"PRIVATE-TOKEN": get_token(), "Content-Type": "application/json",
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
        if exc.code == 401:
            dataset_page = (
                f"https://cds.climate.copernicus.eu/datasets/{cds_id}"
            )
            raise CdsApiError(
                f"CDS API rejected the request for '{cds_id}' (HTTP 401 — permission denied).\n\n"
                "Most likely cause: you need to accept the dataset's licence on the CDS portal.\n\n"
                f"  1. Open: {dataset_page}\n"
                "  2. Scroll to the bottom and click 'Accept Terms'\n"
                "  3. Re-run this script\n\n"
                "If you have already accepted the licence, verify that your API key\n"
                "in ~/.cdsapirc matches the one shown at:\n"
                "  https://cds.climate.copernicus.eu/profile\n\n"
                f"Raw response:\n{body}"
            ) from exc
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
            # Fallback: the OGC API Processes "results" endpoint. This does
            # NOT return the file itself — it returns a JSON document whose
            # actual download URL is nested at asset.value.href.
            results_url = status_url + "/results"
            results = _get_json(results_url)
            try:
                return results["asset"]["value"]["href"]
            except (KeyError, TypeError) as exc:
                raise CdsApiError(
                    f"Could not find a download URL in results response: {results}"
                ) from exc

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

# Leading magic bytes for the formats the CDS API may hand back.
_MAGIC = {
    b"CDF": "netcdf",            # classic NetCDF-3
    b"\x89HDF\r\n\x1a\n": "netcdf",  # NetCDF-4 / HDF5
    b"GRIB": "grib",
    b"PK\x03\x04": "zip",
}


def _sniff_format(path: Path) -> str:
    """Identify a downloaded file by its magic bytes.

    The CDS API ignores ``download_format: unarchived`` when a request spans
    multiple internal streams (e.g. instantaneous + accumulated ERA5
    variables) and returns a ZIP of several files instead.  It may also
    return GRIB when ``data_format`` was not honoured.  We therefore trust
    the bytes on disk, not the requested format.
    """
    with open(path, "rb") as fh:
        head = fh.read(8)
    for magic, fmt in _MAGIC.items():
        if head.startswith(magic):
            return fmt
    return "unknown"


def _open_result_datasets(path: Path) -> list:
    """Open a downloaded CDS result as a list of xarray Datasets.

    Handles NetCDF, GRIB, and ZIP-of-files.  ZIP members are returned as
    separate datasets rather than merged: CDS packs structurally different
    products (e.g. hourly values + monthly statistics) into one ZIP, and
    merging them would union their mismatched ``time`` axes and NaN-pad
    every grid.  The caller turns each dataset's grid variables into bands.
    """
    import xarray as xr

    fmt = _sniff_format(path)

    if fmt == "netcdf":
        return [xr.open_dataset(path, engine="netcdf4")]

    if fmt == "grib":
        try:
            return [xr.open_dataset(path, engine="cfgrib")]
        except (ImportError, ValueError, ModuleNotFoundError) as exc:
            raise CdsApiError(
                "The CDS API returned a GRIB file but cfgrib is not "
                "available to read it.\n"
                "Install with: pip install 'geobridge[full]'\n"
                "or request 'data_format': 'netcdf'."
            ) from exc

    if fmt == "zip":
        import tempfile
        import zipfile

        extract_dir = Path(tempfile.mkdtemp(prefix="cds_zip_"))
        with zipfile.ZipFile(path) as zf:
            zf.extractall(extract_dir)

        members = sorted(
            p for p in extract_dir.rglob("*")
            if p.suffix.lower() in (".nc", ".nc4", ".grib", ".grib2", ".grb")
        )
        if not members:
            raise CdsApiError(
                f"CDS returned a ZIP with no NetCDF/GRIB files: "
                f"{[p.name for p in extract_dir.rglob('*')]}"
            )

        datasets: list = []
        for m in members:
            datasets.extend(_open_result_datasets(m))
        return datasets

    with open(path, "rb") as fh:
        head = fh.read(8)
    raise CdsApiError(
        f"Downloaded file {path.name} is not a recognised format "
        f"(first bytes: {head!r}). "
        "The CDS API may have returned an error page instead of data."
    )


# Dimension-name pieces that identify a latitude / longitude axis. Bare
# "y"/"x" are matched exactly only (see _find), never as substrings.
_LAT_TOKENS = ("latitude", "lat", "rlat", "grid_latitude", "nav_lat")
_LON_TOKENS = ("longitude", "lon", "rlon", "grid_longitude", "nav_lon")


def _spatial_dims(da) -> Optional[tuple[str, str]]:
    """Return ``(lat_dim, lon_dim)`` for a DataArray, or None if it is not a grid.

    Tries dimension names first (``lat``/``latitude``/``rlat``/``y``/…), then
    falls back to each dimension coordinate's ``standard_name`` / ``axis``
    attribute.  Returns None for bounds variables, scalar values, and 1-D
    time series that cannot be rasterised.
    """
    def _find(tokens: tuple, exact: tuple) -> Optional[str]:
        for d in da.dims:
            dl = str(d).lower()
            if dl in exact or dl in tokens:
                return d
        for d in da.dims:
            dl = str(d).lower()
            if any(t in dl for t in tokens):
                return d
        return None

    lat = _find(_LAT_TOKENS, ("y",))
    lon = _find(_LON_TOKENS, ("x",))

    if lat is None or lon is None:
        for name, coord in da.coords.items():
            if name not in da.dims:
                continue
            sn = str(coord.attrs.get("standard_name", "")).lower()
            axis = str(coord.attrs.get("axis", "")).lower()
            units = str(coord.attrs.get("units", "")).lower()
            if lat is None and (sn in ("latitude", "grid_latitude")
                                or axis == "y" or units.startswith("degrees_n")):
                lat = name
            if lon is None and (sn in ("longitude", "grid_longitude")
                                or axis == "x" or units.startswith("degrees_e")):
                lon = name

    if lat is not None and lon is not None and lat != lon:
        return lat, lon
    return None


def _fmt_coord(value: Any) -> str:
    """Compact string for a coordinate value used in a band name."""
    import numpy as np

    if isinstance(value, np.datetime64) or "datetime64" in str(getattr(value, "dtype", "")):
        import pandas as pd
        return pd.Timestamp(value).isoformat()
    if isinstance(value, (np.floating, float)):
        return f"{float(value):g}"
    return str(value)


def _netcdf_to_geotiff(
    nc_path: Path,
    variable: str,
    bbox: Optional[tuple[float, float, float, float]],
    output_path: Path,
    cog: bool = True,
    reduce: str = "stack",
) -> Path:
    """Open a downloaded CDS file, optionally subset, write a GeoTIFF.

    ``reduce`` controls what happens to non-spatial dimensions (time, level,
    ensemble number) left in each variable:

    * ``"stack"`` (default) — every (time, level, …) combination becomes its
      own band, named ``<variable>  <dim>=<value>  …``.
    * ``"mean"`` — collapse them with an average, giving one band per
      variable.
    """
    try:
        import xarray as xr  # noqa: F401
        import rioxarray  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "xarray and rioxarray are required for NetCDF conversion.\n"
            "Install with: pip install 'geobridge[zarr]'"
        ) from exc

    import xarray as xr

    if reduce not in ("stack", "mean"):
        raise ValueError(f"reduce must be 'stack' or 'mean', got {reduce!r}")

    datasets = _open_result_datasets(nc_path)

    # Resolve the requested variable against the file. CDS NetCDF uses short
    # names ('sp', 'tp', 't2m') while requests use long names
    # ('surface_pressure', ...), so also match on the variables' attributes.
    def _norm(s: str) -> str:
        # Collapse any run of non-alphanumerics to a single underscore so
        # "Leaf area index, high vegetation" == "leaf_area_index_high_vegetation".
        return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

    def _matches(want: str, var_name: str, da: xr.DataArray) -> bool:
        target = _norm(want)
        ids = {_norm(var_name)}
        for key in ("long_name", "standard_name", "GRIB_name", "GRIB_cfName"):
            val = da.attrs.get(key)
            if val:
                ids.add(_norm(str(val)))
        return target in ids

    def _display_name(var_name: str, da: xr.DataArray) -> str:
        """Human-readable variable name for band labels.

        CDS files store short names ('r', 't2m', 'z'); prefer the descriptive
        ``long_name`` / ``standard_name`` attribute when present.
        """
        for key in ("long_name", "standard_name", "GRIB_name"):
            val = da.attrs.get(key)
            if val:
                return str(val)
        return var_name

    # Collect every grid variable across all downloaded files. Non-grid
    # variables (bounds, scalar statistics, 1-D time series) are skipped —
    # they cannot be rasterised.
    candidates: list = []          # (var_name, DataArray, (lat_dim, lon_dim))
    skipped: list = []             # (var_name, dims)
    for d in datasets:
        for name, da in d.data_vars.items():
            sp = _spatial_dims(da)
            if sp is None:
                skipped.append((str(name), tuple(str(x) for x in da.dims)))
                continue
            candidates.append((str(name), da, sp))

    if not candidates:
        raise CdsApiError(
            "None of the downloaded variables are lat/lon grids that can be "
            f"written to GeoTIFF. Variables found: "
            f"{['%s%s' % (n, dims) for n, dims in skipped]}"
        )
    if skipped:
        logger.info(
            "Skipping non-grid variables: %s",
            ", ".join(f"{n}{dims}" for n, dims in skipped),
        )

    # Narrow to the requested variable when it matches at least one grid var.
    if variable:
        matched = [
            c for c in candidates
            if c[0] == variable or _matches(variable, c[0], c[1])
        ]
        if matched:
            candidates = matched
        elif len(candidates) == 1:
            # The file holds exactly one grid variable; it must be the one
            # that was requested even though the names don't line up.
            logger.info(
                "Requested variable '%s' stored as '%s' in the downloaded file.",
                variable, candidates[0][0],
            )
        else:
            logger.warning(
                "Variable '%s' not found among grid variables %s — writing all.",
                variable, [c[0] for c in candidates],
            )

    def _bands(name: str, da: xr.DataArray):
        """Yield (label, 2-D DataArray) pairs for one variable."""
        extra_dims = [d for d in da.dims if d not in ("x", "y")]

        if not extra_dims:
            yield name, da.reset_coords(drop=True)
            return

        if reduce == "mean":
            logger.info("Collapsing %s by mean for '%s'", extra_dims, name)
            yield name, da.mean(dim=extra_dims).reset_coords(drop=True)
            return

        # reduce == "stack": one band per (time, level, …) combination
        import itertools
        for idx in itertools.product(*(range(da.sizes[d]) for d in extra_dims)):
            sub = da.isel(dict(zip(extra_dims, idx)))
            suffix = "  ".join(
                f"{d}={_fmt_coord(sub[d].values)}" for d in extra_dims if d in sub.coords
            )
            label = f"{name}  {suffix}" if suffix else name
            yield label, sub.reset_coords(drop=True)

    bands: list = []
    labels: list[str] = []
    for name, raw, (lat_dim, lon_dim) in candidates:
        da = raw.rename({lat_dim: "y", lon_dim: "x"})

        # A regular lat/lon grid has 1-D coordinate values on both axes.
        # Projected / curvilinear grids (CERRA and other regional reanalyses
        # on Lambert, polar-stereographic, … grids) arrive with bare y/x
        # dimensions plus 2-D lat/lon arrays — geobridge cannot turn those
        # into a georeferenced GeoTIFF, so fail loudly rather than write an
        # un-georeferenced raster.
        if "y" not in da.coords or "x" not in da.coords or da["y"].ndim != 1:
            grid_type = raw.attrs.get("GRIB_gridType", "unknown")
            raise CdsApiError(
                f"'{name}' is on a projected or curvilinear grid "
                f"(GRIB_gridType={grid_type!r}). geobridge only converts "
                "datasets on a regular latitude/longitude grid to GeoTIFF; "
                "regional reanalyses such as CERRA (Lambert Conformal) are "
                "not supported by this path."
            )

        if bbox:
            west, south, east, north = bbox
            descending = float(da.y[0]) > float(da.y[-1])
            da = da.sel(
                y=slice(north, south) if descending else slice(south, north),
                x=slice(west, east),
            )
            if da.sizes.get("y", 0) == 0 or da.sizes.get("x", 0) == 0:
                raise CdsApiError(
                    f"Spatial subset {bbox} selects no data from '{name}'. "
                    "bbox must be (west, south, east, north) and overlap the "
                    "request area."
                )

        for label, band in _bands(_display_name(name, da), da):
            labels.append(label)
            bands.append(band.rename("band"))

    if len(bands) == 1:
        da = bands[0]
    else:
        ref = bands[0]
        if any(b.sizes != ref.sizes for b in bands[1:]):
            logger.warning(
                "Downloaded files have different grids; aligning all bands "
                "to '%s' by nearest neighbour.", labels[0],
            )
            bands = [ref] + [
                b.reindex(y=ref["y"], x=ref["x"], method="nearest")
                for b in bands[1:]
            ]
        da = xr.concat(bands, dim="band", combine_attrs="drop_conflicts")
        da = da.assign_coords(band=range(1, len(bands) + 1))
    # rioxarray writes this as per-band descriptions (GDAL band metadata)
    da.attrs["long_name"] = tuple(labels)

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
    reduce: str = "stack",
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
        Variable name to extract from the downloaded file.  If None (or not
        found in the file), every variable in the file is written, one or
        more bands each.
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
    reduce : {"stack", "mean"}
        How to handle non-spatial dimensions (time, level, ensemble number).
        ``"stack"`` (default) writes one band per (variable, time, level, …)
        combination; ``"mean"`` averages them into one band per variable.

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

    # Infer variable from request only when it is unambiguous — a single
    # requested variable.  With several, leave it None so every variable in
    # the downloaded file is written.
    if variable is None:
        vars_in_request = request.get("variable", [])
        if isinstance(vars_in_request, str):
            variable = vars_in_request
        elif isinstance(vars_in_request, list) and len(vars_in_request) == 1:
            variable = vars_in_request[0]

    # Validate before submitting to catch errors without burning queue time
    try:
        from geobridge.modules.form import validate_request
        errors = validate_request(dataset, request)
        if errors:
            raise CdsApiError(
                f"Invalid request for '{dataset}':\n"
                + "\n".join(f"  • {e}" for e in errors)
            )
    except ImportError:
        pass

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
            nc_path, variable or "", bbox, output_path, cog=cog, reduce=reduce
        )
    finally:
        nc_path.unlink(missing_ok=True)

    if progress_callback:
        progress_callback(f"Done: {result.name}")

    return result
