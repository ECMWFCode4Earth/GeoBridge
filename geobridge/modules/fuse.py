"""
geobridge.modules.fuse
~~~~~~~~~~~~~~~~~~~~~~

Multi-service layer co-registration.

Spatially co-registers two layers from any combination of C3S, CAMS, and
CEMS, resolving grid resolution mismatches automatically. This module solves
the burden of combining ERA5 (0.25°), CAMS (0.4°), and CEMS (1 km) data at a
common grid for analysis.

Co-registration is spatial only. Time is not aligned: both layers must already
describe the same period (for example by extracting both with the same
``aggregation=``), otherwise bands that do not correspond are paired.

The resampling kernel is chosen per layer from what the variable means and
whether the grid is being coarsened or refined; see
:mod:`geobridge.modules.resampling`.

Requires the ``[zarr]`` optional dependencies for full functionality:
    pip install "geobridge[zarr]"
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Union

from geobridge.modules.resampling import resolve_method, to_rasterio

logger = logging.getLogger(__name__)

PathLike = Union[str, os.PathLike]


# ---------------------------------------------------------------------------
# Lazy imports for optional dependencies
# ---------------------------------------------------------------------------

def _require_xarray():
    """Import xarray and rioxarray on demand."""
    try:
        import xarray as xr
        import rioxarray  # noqa: F401
        import numpy as np
        return xr, np
    except ImportError as exc:
        raise RuntimeError(
            "fuse() requires the [zarr] extras. Install with:\n"
            "    pip install 'geobridge[zarr]'"
        ) from exc


# ---------------------------------------------------------------------------
# FusedLayer dataclass
# ---------------------------------------------------------------------------

@dataclass
class FusedLayer:
    """
    Two co-registered layers stored as a multi-band xarray Dataset.

    Provides analysis helpers (correlation, scatter) and export to
    multi-band GeoTIFF.
    """

    data: object  # xarray.Dataset, but kept as object to avoid hard import
    name_a: str
    name_b: str
    resolution_deg: float
    bbox: tuple[float, float, float, float]
    metadata: dict = field(default_factory=dict)

    def correlation(self) -> float:
        """
        Return the Pearson correlation coefficient between the two layers.

        The coefficient is descriptive only. Neighbouring pixels are spatially
        autocorrelated, so they are not independent samples and the effective
        sample size is far smaller than the pixel count; any p-value or
        confidence interval read off this r will overstate significance.
        Pixels are also weighted equally rather than by area, which is
        negligible over a city but not over a continental extent.
        """
        _, np = _require_xarray()
        a = self.data[self.name_a].values.flatten()
        b = self.data[self.name_b].values.flatten()
        mask = np.isfinite(a) & np.isfinite(b)
        if mask.sum() < 2:
            return float("nan")
        return float(np.corrcoef(a[mask], b[mask])[0, 1])

    def scatter_data(self) -> tuple:
        """Return paired (a, b) finite values as numpy arrays for plotting."""
        _, np = _require_xarray()
        a = self.data[self.name_a].values.flatten()
        b = self.data[self.name_b].values.flatten()
        mask = np.isfinite(a) & np.isfinite(b)
        return a[mask], b[mask]

    def to_geotiff(self, output_path: PathLike, cog: bool = True) -> Path:
        """Write the fused dataset as a multi-band Cloud Optimized GeoTIFF."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Stack the two variables along a band dimension
        xr, _ = _require_xarray()
        stacked = xr.concat(
            [self.data[self.name_a], self.data[self.name_b]],
            dim="band",
        )
        stacked = stacked.assign_coords(band=[self.name_a, self.name_b])
        stacked.attrs["long_name"] = (self.name_a, self.name_b)

        if stacked.rio.crs is None:
            stacked = stacked.rio.write_crs("EPSG:4326")

        if cog:
            stacked.rio.to_raster(
                output_path, driver="COG",
                compress="DEFLATE", BLOCKSIZE=512,
            )
        else:
            stacked.rio.to_raster(
                output_path, driver="GTiff",
                compress="DEFLATE", tiled=True,
            )
        return output_path

    def __repr__(self) -> str:
        return (
            f"FusedLayer({self.name_a!r} vs {self.name_b!r}, "
            f"res={self.resolution_deg}°, "
            f"bbox={self.bbox})"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_grid_resolution(
    da_a, da_b, resolution: Union[str, float],
) -> float:
    """Pick a target grid resolution in degrees."""
    res_a = _detect_resolution(da_a)
    res_b = _detect_resolution(da_b)

    if isinstance(resolution, (int, float)):
        return float(resolution)
    if resolution == "finer":
        return min(res_a, res_b)
    if resolution == "coarser":
        return max(res_a, res_b)
    raise ValueError(
        f"resolution must be 'finer', 'coarser', or a float; got {resolution!r}"
    )


def _detect_resolution(da) -> float:
    """Estimate the spatial resolution of an xarray DataArray in degrees."""
    _, np = _require_xarray()
    for axis in ("x", "longitude", "lon"):
        if axis in da.coords:
            values = da[axis].values
            if len(values) > 1:
                return float(abs(np.diff(values).mean()))
    return 0.25  # Fallback: ERA5 native resolution


def _common_target_grid(
    da_a, da_b, resolution_deg: float,
    bbox: Optional[tuple[float, float, float, float]] = None,
):
    """
    Construct an xarray DataArray with the target grid coordinates.

    Latitudes run north to south (descending), matching the north-up GeoTIFF
    convention, so the reprojected layers write out with a standard transform.
    """
    xr, np = _require_xarray()

    if bbox is None:
        # Compute the intersection of both bounding boxes
        xa = _coord(da_a, "x")
        ya = _coord(da_a, "y")
        xb = _coord(da_b, "x")
        yb = _coord(da_b, "y")
        west = max(float(xa.min()), float(xb.min()))
        east = min(float(xa.max()), float(xb.max()))
        south = max(float(ya.min()), float(yb.min()))
        north = min(float(ya.max()), float(yb.max()))
        bbox = (west, south, east, north)

    west, south, east, north = bbox
    if east <= west or north <= south:
        raise ValueError(
            f"Empty bbox after intersection: {bbox}. "
            "The two layers may have non-overlapping coverage."
        )

    lons = np.arange(west, east + resolution_deg / 2, resolution_deg)
    lats = np.arange(north, south - resolution_deg / 2, -resolution_deg)
    target = xr.DataArray(
        np.zeros((len(lats), len(lons))),
        coords={"y": lats, "x": lons},
        dims=("y", "x"),
    ).rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=False)
    return target.rio.write_crs("EPSG:4326")


def _coord(da, name: str):
    """Return the coordinate array for x or y under any common name."""
    aliases = {
        "x": ("x", "longitude", "lon"),
        "y": ("y", "latitude", "lat"),
    }
    for alias in aliases[name]:
        if alias in da.coords:
            return da[alias]
    raise KeyError(f"No coordinate found for axis {name!r} in DataArray")


def _semantic_hints(da, *names) -> list[str]:
    """
    Strings that may identify the variable in ``da``, most specific first.

    GeoTIFFs written by geobridge carry the variable in the file stem and in
    the per-band description (``long_name``); CDS-derived arrays may also carry
    ``standard_name`` or a GRIB short name.
    """
    hints = [n for n in names if n]
    for key in ("long_name", "standard_name", "GRIB_shortName", "GRIB_name"):
        value = da.attrs.get(key)
        if isinstance(value, (tuple, list)):
            value = value[0] if value else None
        if value:
            hints.append(str(value))
    return hints


def _normalize_input(layer):
    """
    Accept any of: xarray.DataArray, path to GeoTIFF, WmtsLayer, LayerDescriptor.
    Return an xarray.DataArray with .rio accessor and a name string.
    """
    xr, _ = _require_xarray()

    # Already an xarray DataArray
    if hasattr(layer, "dims") and hasattr(layer, "coords"):
        name = getattr(layer, "name", None) or "layer"
        return layer, name

    # GeoTIFF path
    if isinstance(layer, (str, os.PathLike)):
        path = Path(layer)
        if path.exists() and path.suffix.lower() in (".tif", ".tiff"):
            da = xr.open_rasterio(str(path)) if hasattr(xr, "open_rasterio") \
                else __import__("rioxarray").open_rasterio(str(path))
            return da, path.stem

    # WmtsLayer or LayerDescriptor — fuse needs raster data, not WMTS tiles
    if hasattr(layer, "variable") and hasattr(layer, "dataset"):
        raise TypeError(
            f"fuse() needs raster data, not a WMTS layer reference. "
            f"Use gb.zarr_to_geotiff() to extract {layer.dataset!r} as a "
            f"GeoTIFF first, then pass that path to fuse()."
        )

    raise TypeError(
        f"Cannot fuse object of type {type(layer).__name__}. "
        "Pass an xarray.DataArray or a GeoTIFF path."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fuse(
    layer_a,
    layer_b,
    resolution: Union[str, float] = "coarser",
    bbox: Optional[tuple[float, float, float, float]] = None,
    name_a: Optional[str] = None,
    name_b: Optional[str] = None,
    method: str = "auto",
    method_a: Optional[str] = None,
    method_b: Optional[str] = None,
) -> FusedLayer:
    """
    Spatially co-register two raster layers onto a common grid.

    Only the spatial grid is aligned; both layers must already cover the same
    time period.

    Parameters
    ----------
    layer_a, layer_b : xarray.DataArray or path to GeoTIFF
        The two layers to fuse. Must have CRS metadata.
    resolution : 'finer' | 'coarser' | float
        Target grid resolution in degrees, or one of the keywords:
        'finer'   — use the finer of the two source resolutions
        'coarser' — use the coarser (default — safer for analysis)
    bbox : tuple, optional
        Restrict the fused output to (west, south, east, north).
        Defaults to the intersection of the two input bounding boxes.
    name_a, name_b : str, optional
        Names for the two bands in the resulting Dataset. Default to
        ``layer_a.name`` / ``layer_b.name`` or 'layer_a' / 'layer_b'.
    method : 'auto' | 'nearest' | 'bilinear' | 'cubic' | 'average' | 'mode' | 'max' | 'min' | 'med'
        Resampling kernel applied to both layers. ``'auto'`` (default) picks
        per layer from the variable's meaning, inferred from its name, and
        from whether the grid is being coarsened or refined:

        ===========  ==============  ==========
        semantics    downsampling    upsampling
        ===========  ==============  ==========
        continuous   average         bilinear
        categorical  mode            nearest
        extrema      max (min)       nearest
        ===========  ==============  ==========

    method_a, method_b : str, optional
        Per-layer override of ``method``.

    Returns
    -------
    FusedLayer
        Co-registered multi-band layer with .correlation(), .scatter_data(),
        and .to_geotiff() methods. The kernel used for each layer, and why,
        is recorded in ``metadata["resampling"]``.

    Notes
    -----
    ``average`` preserves the areal mean but not summed totals, so
    precipitation and fluxes need conservative regridding (e.g. xESMF) if
    totals must be preserved exactly. Semantics inference is a name heuristic
    that defaults to continuous; an oddly named categorical layer needs
    ``method_a='nearest'`` (or ``method_b``). Use
    :func:`geobridge.modules.resampling.check_resampling` to audit a result.

    Examples
    --------
    >>> import geobridge as gb
    >>> gb.authenticate()
    >>> temp_tif = gb.zarr_to_geotiff(
    ...     "reanalysis-era5-single-levels", "2m_temperature",
    ...     bbox=(23.5, 37.8, 24.1, 38.1),
    ...     time_range=("2023-07-01", "2023-07-31"),
    ...     aggregation="monthly_mean")
    >>> pm_tif = gb.zarr_to_geotiff(
    ...     "cams-europe-air-quality-reanalyses", "pm2p5",
    ...     bbox=(23.5, 37.8, 24.1, 38.1),
    ...     time_range=("2023-07-01", "2023-07-31"),
    ...     aggregation="monthly_mean")
    >>> fused = gb.fuse(temp_tif, pm_tif)
    >>> print(f"Correlation: {fused.correlation():.2f}")
    >>> fused.to_geotiff("./fused_temp_pm25.tif")
    """
    xr, _ = _require_xarray()

    da_a, default_name_a = _normalize_input(layer_a)
    da_b, default_name_b = _normalize_input(layer_b)

    # Ensure both have CRS info
    if da_a.rio.crs is None:
        da_a = da_a.rio.write_crs("EPSG:4326")
    if da_b.rio.crs is None:
        da_b = da_b.rio.write_crs("EPSG:4326")

    # Reproject both to EPSG:4326 if not already
    if str(da_a.rio.crs) != "EPSG:4326":
        da_a = da_a.rio.reproject("EPSG:4326")
    if str(da_b.rio.crs) != "EPSG:4326":
        da_b = da_b.rio.reproject("EPSG:4326")

    # Drop the band dimension if present (common when reading multi-band TIFF)
    if "band" in da_a.dims and da_a.sizes["band"] == 1:
        da_a = da_a.isel(band=0, drop=True)
    if "band" in da_b.dims and da_b.sizes["band"] == 1:
        da_b = da_b.isel(band=0, drop=True)

    target_res = _resolve_grid_resolution(da_a, da_b, resolution)
    target_grid = _common_target_grid(da_a, da_b, target_res, bbox)

    n_a = name_a or default_name_a or "layer_a"
    n_b = name_b or default_name_b or "layer_b"
    if n_a == n_b:
        n_b = n_b + "_b"

    # The kernel follows what the variable means and which way the grid moves
    choice_a = resolve_method(
        _semantic_hints(da_a, name_a, default_name_a),
        _detect_resolution(da_a), target_res, method_a or method, label=n_a,
    )
    choice_b = resolve_method(
        _semantic_hints(da_b, name_b, default_name_b),
        _detect_resolution(da_b), target_res, method_b or method, label=n_b,
    )
    logger.debug("fuse resampling: %s -> %s, %s -> %s",
                 n_a, choice_a.method, n_b, choice_b.method)

    a_resampled = da_a.rio.reproject_match(
        target_grid, resampling=to_rasterio(choice_a.method))
    b_resampled = da_b.rio.reproject_match(
        target_grid, resampling=to_rasterio(choice_b.method))

    fused_ds = xr.Dataset({n_a: a_resampled, n_b: b_resampled})
    if fused_ds.rio.crs is None:
        fused_ds = fused_ds.rio.write_crs("EPSG:4326")

    final_bbox = (
        float(target_grid.x.min()), float(target_grid.y.min()),
        float(target_grid.x.max()), float(target_grid.y.max()),
    )
    return FusedLayer(
        data=fused_ds,
        name_a=n_a,
        name_b=n_b,
        resolution_deg=target_res,
        bbox=final_bbox,
        metadata={
            "resampling": {n_a: asdict(choice_a), n_b: asdict(choice_b)},
            "target_crs": "EPSG:4326",
        },
    )
