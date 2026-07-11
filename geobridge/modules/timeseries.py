"""
geobridge.modules.timeseries
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Extract a value time series at a single point via WMTS GetFeatureInfo.

No data is downloaded — each time step is a single lightweight
GetFeatureInfo request against the ECMWF WMTS service
(``https://wmts.datastores.ecmwf.int/teroWmts``), which returns the raw
cell value as JSON. This is the right tool for "what was the value here,
over time" at a handful of points; for spatial subsets or long series at
many points, use :func:`geobridge.zarr_to_geotiff` instead (see its
module docstring for the ``time_chunked`` vs ``geo_chunked`` trade-off).
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
