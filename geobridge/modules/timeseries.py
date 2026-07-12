"""
geobridge.modules.timeseries
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Extract a value time series at a single point — two ways.

:func:`point_time_series` uses WMTS GetFeatureInfo: no data is
downloaded, each time step is a single lightweight request against the
ECMWF WMTS service (``https://wmts.datastores.ecmwf.int/teroWmts``),
which returns the raw cell value as JSON. Good for exploratory queries
over a handful to a few dozen time steps. Past that, firing one HTTP
request per time step in a tight loop can trip server-side rate
limiting (observed as connection resets/"remote end closed connection"
under sustained use) — there is no way to fix that from the client side
beyond pacing and retries, since the bottleneck is request *count*, not
any one request being slow.

:func:`zarr_point_time_series` reads the same point directly out of the
ARCO Zarr archive instead — a handful of chunked HTTPS range-requests
covering the whole time range in one access, rather than one request per
time step. This is the one to prefer for long ranges; see its docstring
for the ``geo_chunked`` vs ``time_chunked`` trade-off (the same one
:func:`geobridge.zarr_to_geotiff` documents, except a point query always
wants ``geo_chunked`` when it's available).
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Union

from geobridge.modules import extract
from geobridge.modules.discover import discover_one
from geobridge.modules.wmts import wmts_layer, WmtsLayer

logger = logging.getLogger(__name__)

DateLike = Union[str, datetime]

_TILE_SIZE = 256
_USER_AGENT = "geobridge"
_REQUEST_TIMEOUT = 30


class TimeSeriesError(RuntimeError):
    """Raised when a GetFeatureInfo request or response is invalid."""


@dataclass
class PointSample:
    time: datetime
    value: Optional[float]


# ---------------------------------------------------------------------------
# Tile math
# ---------------------------------------------------------------------------

def _lonlat_to_tile_pixel(lon: float, lat: float, zoom: int) -> tuple[int, int, int, int]:
    """Convert lon/lat to (col, row, pixel_i, pixel_j) for Web Mercator (EPSG:3857).

    Standard slippy-map tile scheme: 2**zoom tiles per axis, 256px tiles,
    origin at the top-left. This matches the XYZ tile convention the WMTS
    module already relies on for QGIS (see wmts.py module docstring).
    """
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n

    col = int(x)
    row = int(y)
    pixel_i = int((x - col) * _TILE_SIZE)
    pixel_j = int((y - row) * _TILE_SIZE)
    return col, row, pixel_i, pixel_j


# ---------------------------------------------------------------------------
# GetFeatureInfo request
# ---------------------------------------------------------------------------

def _fetch_value(url: str) -> Optional[float]:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TimeSeriesError(f"GetFeatureInfo request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TimeSeriesError(f"GetFeatureInfo response was not valid JSON: {exc}") from exc

    features = data.get("features") or []
    if not features:
        return None
    return features[0].get("properties", {}).get("value")


def point_value(
    dataset: str,
    variable: str,
    lon: float,
    lat: float,
    time: DateLike,
    zoom: int = 8,
    style: str = "default",
    descriptor=None,
) -> Optional[float]:
    """Query the cell value at a single point and time via GetFeatureInfo.

    No tiles or arrays are downloaded — only a small JSON response.

    Parameters
    ----------
    dataset, variable : str
        Same accepted forms as :func:`geobridge.wmts_layer`.
    lon, lat : float
        Point location in WGS-84 degrees.
    time : str or datetime
        Time slice to query.
    zoom : int
        WMTS zoom level (0-10). Higher zoom narrows the cell the returned
        value represents; 8 is a reasonable default for point queries.
    style : str
        WMTS style, passed through to :func:`geobridge.wmts_layer`.
    descriptor : LayerDescriptor, optional
        Pre-fetched descriptor to avoid repeated catalogue lookups when
        calling this in a loop (see :func:`point_time_series`).

    Returns
    -------
    float or None
        The cell value, or ``None`` if the point falls outside the data
        mask (e.g. ocean for a land-only variable).
    """
    layer = wmts_layer(dataset, variable, time, style=style, descriptor=descriptor)
    col, row, i, j = _lonlat_to_tile_pixel(lon, lat, zoom)
    url = layer.feature_info_url(zoom, col, row, i, j)
    return _fetch_value(url)


def point_time_series(
    dataset: str,
    variable: str,
    lon: float,
    lat: float,
    start: DateLike,
    end: DateLike,
    step: timedelta = timedelta(days=1),
    zoom: int = 8,
    style: str = "default",
) -> list[PointSample]:
    """Extract a value time series at a point using WMTS GetFeatureInfo.

    Issues one GetFeatureInfo request per time step — nothing is
    downloaded or cached locally. Good for exploratory point queries over
    a handful to a few hundred time steps; for dense series prefer
    :func:`geobridge.zarr_to_geotiff` with a tight bbox, which reads the
    ``geo_chunked`` ARCO archive built for exactly that access pattern.

    Parameters
    ----------
    dataset, variable : str
        Same accepted forms as :func:`geobridge.wmts_layer`.
    lon, lat : float
        Point location in WGS-84 degrees.
    start, end : str or datetime
        Inclusive time range.
    step : timedelta
        Spacing between queried time steps. Default one day.
    zoom : int
        WMTS zoom level (0-10). Default 8.
    style : str
        WMTS style, passed through to :func:`geobridge.wmts_layer`.

    Returns
    -------
    list of PointSample
        One entry per queried time step, in chronological order.

    Examples
    --------
    >>> import geobridge as gb
    >>> from datetime import datetime, timedelta
    >>> series = gb.point_time_series(
    ...     "reanalysis_era5_single_levels", "t2m",
    ...     lon=23.7, lat=38.0,
    ...     start=datetime(2023, 1, 1), end=datetime(2023, 12, 31),
    ...     step=timedelta(days=30),
    ... )
    >>> for sample in series:
    ...     print(sample.time, sample.value)
    """
    if isinstance(start, str):
        start = datetime.fromisoformat(start.replace("Z", "+00:00") if "T" in start else start)
    if isinstance(end, str):
        end = datetime.fromisoformat(end.replace("Z", "+00:00") if "T" in end else end)

    descriptor = discover_one(dataset)
    if descriptor is None:
        raise TimeSeriesError(
            f"Could not discover dataset {dataset!r}. "
            "Check the identifier or call gb.discover() to list options."
        )

    samples: list[PointSample] = []
    current = start
    while current <= end:
        value = point_value(
            dataset, variable, lon, lat, current,
            zoom=zoom, style=style, descriptor=descriptor,
        )
        samples.append(PointSample(time=current, value=value))
        current += step

    return samples


# ---------------------------------------------------------------------------
# Point time series — ARCO Zarr path (bulk read, not one request per step)
# ---------------------------------------------------------------------------

def zarr_point_time_series(
    dataset: str,
    variable: str,
    lon: float,
    lat: float,
    start: DateLike,
    end: DateLike,
    chunking: Optional[str] = None,
) -> list[PointSample]:
    """Extract a value time series at a point from the ARCO Zarr archive.

    Reads the point's nearest grid cell directly out of the Zarr store
    across the whole ``start``-``end`` range in one lazy-then-computed
    access — a handful of chunked HTTPS range-requests, rather than
    :func:`point_time_series`'s one WMTS GetFeatureInfo request per time
    step. Prefer this for long ranges (more than a few dozen steps):
    GetFeatureInfo's per-timestep design can trip server-side rate
    limiting past a certain request volume, which shows up as connection
    resets — this doesn't have that failure mode, since it isn't opening
    one connection per timestep in the first place.

    Defaults to the ``geo_chunked`` archive flavour whenever the dataset
    has one: chunked along the spatial axes with a long run of time per
    chunk, exactly the "tiny area, long time" shape a point query always
    is. This is different from :func:`geobridge.zarr_to_geotiff`, which
    picks between ``geo_chunked``/``time_chunked`` based on the *bbox*
    size of a spatial-map request — a point is a degenerate bbox, so it
    always wants the point-shaped archive regardless of range length.

    Requires the ``[zarr]`` optional dependencies, same as
    :func:`geobridge.zarr_to_geotiff`::

        pip install "geobridge[zarr]"

    Parameters
    ----------
    dataset : str
        Dataset identifier, e.g. ``'reanalysis-era5-single-levels'``.
        Must be present in the GeoBridge ARCO catalogue.
    variable : str
        Variable name in either CDS long form (``'2m_temperature'``) or
        ARCO short form (``'t2m'``).
    lon, lat : float
        Point location in WGS-84 degrees. Snapped to the nearest grid
        cell — there is no interpolation between cells.
    start, end : str or datetime
        Inclusive time range.
    chunking : {'time_chunked', 'geo_chunked'}, optional
        Force a specific archive flavour. Default: ``geo_chunked`` if the
        dataset has one, else ``time_chunked``.

    Returns
    -------
    list of PointSample
        One entry per time step found in the archive within the range,
        in chronological order. A NaN cell value in the underlying array
        (e.g. a land-only variable queried over ocean) becomes
        ``value=None`` — same convention as :func:`point_value`.

    Raises
    ------
    ExtractionError
        If the dataset/variable/archive access fails — the same failure
        modes as :func:`geobridge.zarr_to_geotiff`, since this reads the
        same archive.

    Examples
    --------
    >>> import geobridge as gb
    >>> gb.authenticate()
    >>> series = gb.zarr_point_time_series(
    ...     dataset="reanalysis-era5-single-levels",
    ...     variable="2m_temperature",
    ...     lon=23.7, lat=38.0,
    ...     start="2010-01-01", end="2023-12-31",
    ... )
    >>> for sample in series[:3]:
    ...     print(sample.time, sample.value)
    """
    xr, np = extract._require_xarray()
    import pandas as pd

    entry = extract._get_dataset_entry(dataset)
    short_var = extract._resolve_variable_name(entry, variable)

    zarr_urls = entry.get("zarr", {})
    flavour = chunking or ("geo_chunked" if "geo_chunked" in zarr_urls else "time_chunked")
    if flavour not in zarr_urls:
        raise extract.ExtractionError(
            f"Chunking flavour {flavour!r} not available for dataset "
            f"{dataset!r}. Available: {list(zarr_urls)}"
        )
    zarr_url = zarr_urls[flavour]
    logger.info("Using %s archive for point time series on %s", flavour, dataset)

    ds = extract._open_arco_zarr(zarr_url)

    if short_var not in ds.data_vars:
        available = list(ds.data_vars)[:20]
        raise extract.ExtractionError(
            f"Variable {short_var!r} not in Zarr store. "
            f"First 20 available variables: {available}"
        )
    da = ds[short_var]

    lon_name = extract._coord_name(da, ("longitude", "lon", "x"))
    lat_name = extract._coord_name(da, ("latitude", "lat", "y"))
    time_name = extract._coord_name(da, ("time", "valid_time", "t"))
    if lon_name is None or lat_name is None or time_name is None:
        raise extract.ExtractionError(
            "Could not find longitude/latitude/time coordinates. "
            f"Available coords: {list(da.coords)}"
        )

    query_lon = lon
    if entry.get("longitude_convention") == "zero_to_360" and query_lon < 0:
        query_lon += 360

    point_da = da.sel({lon_name: query_lon, lat_name: lat}, method="nearest")
    point_da = point_da.sel({time_name: slice(start, end)})

    if point_da.sizes.get(time_name, 0) == 0:
        raise extract.ExtractionError(
            f"No timesteps found for {dataset!r} between {start!r} and {end!r}.\n"
            f"Dataset coverage: {entry.get('time_start')} to {entry.get('time_end')}"
        )

    times = point_da[time_name].values
    values = point_da.values  # triggers the actual chunk fetch + compute

    samples: list[PointSample] = []
    for t, v in zip(times, values):
        py_time = pd.Timestamp(t).to_pydatetime()
        py_value = None if (v is None or bool(np.isnan(v))) else float(v)
        samples.append(PointSample(time=py_time, value=py_value))
    return samples
