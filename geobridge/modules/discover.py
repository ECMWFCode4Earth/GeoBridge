"""
geobridge.modules.discover
~~~~~~~~~~~~~~~~~~~~~~~~~~

Dataset catalogue discovery for Copernicus services.

Reads the locally bundled STAC catalogue snapshot
(``geobridge/semantic/cds_snapshot.yaml``) and the access overrides
(``geobridge/semantic/arco_overrides.yaml``) to produce unified
``LayerDescriptor`` objects that any GIS platform can consume.

The snapshot is refreshed periodically by the maintainer-side script
``scripts/refresh_catalogue.py``.  Users never refresh it themselves;
they get whatever snapshot was bundled with the installed version of
GeoBridge.

Why a snapshot rather than live STAC queries
--------------------------------------------
ECMWF's STAC catalogue is the authoritative source, but querying it
at runtime would introduce a hard network dependency on every call to
:func:`discover`.  Snapshotting keeps GeoBridge offline-by-default
and makes the catalogue a versioned part of the library.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SNAPSHOT_PATH = (
    Path(__file__).parent.parent / "semantic" / "cds_snapshot.yaml"
)
_OVERRIDES_PATH = (
    Path(__file__).parent.parent / "semantic" / "arco_overrides.yaml"
)

_VARIABLE_COLORMAPS: dict[str, dict] = {
    "2m_temperature":      {"palette": "RdBu_r",   "unit": "K"},
    "t2m":                 {"palette": "RdBu_r",   "unit": "K"},
    "total_precipitation": {"palette": "YlGnBu",   "unit": "m"},
    "tp":                  {"palette": "YlGnBu",   "unit": "m"},
    "pm2p5":               {"palette": "YlOrRd",   "unit": "kg/m³"},
    "no2":                 {"palette": "Purples",  "unit": "kg/m³"},
    "utci":                {"palette": "RdYlBu_r", "unit": "K"},
    "sst":                 {"palette": "RdBu_r",   "unit": "K"},
    "skt":                 {"palette": "RdBu_r",   "unit": "K"},
}

_DEFAULT_COLORMAP = {"palette": "viridis", "unit": ""}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TileMatrixSet:
    """Describes a WMTS tile matrix set (CRS plus zoom levels)."""

    identifier: str
    crs: str
    min_zoom: int = 0
    max_zoom: int = 10

    @property
    def epsg_code(self) -> Optional[str]:
        # Matches both "EPSG:3857" and "urn:ogc:def:crs:EPSG::3857"
        match = re.search(r"EPSG[:/]+(\d+)", self.crs, re.IGNORECASE)
        return f"EPSG:{match.group(1)}" if match else None


@dataclass
class LayerDescriptor:
    """Unified description of a Copernicus dataset.

    Combines STAC metadata (id, title, extent, keywords) with the access
    details GeoBridge maintains in ``arco_overrides.yaml``.

    Branch on :attr:`has_zarr` / :attr:`has_wmts` before calling any
    access-method-specific code — not every dataset supports both.
    """

    # Core metadata
    id: str
    title: str
    service: str
    abstract: str = ""
    variables: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    license: str = "unknown"
    providers: list[str] = field(default_factory=list)
    thumbnail: str = ""

    # Spatial / temporal extent
    bbox: tuple[float, float, float, float] = (-180.0, -90.0, 180.0, 90.0)
    crs: str = "EPSG:4326"
    time_range: tuple[datetime, datetime] = field(
        default_factory=lambda: (
            datetime(1940, 1, 1, tzinfo=timezone.utc),
            datetime.now(timezone.utc),
        )
    )
    time_step: str = "1h"

    # Access methods — None / empty means not available for this dataset
    zarr_time_chunked: Optional[str] = None
    zarr_geo_chunked:  Optional[str] = None
    wmts_url: str = ""
    wmts_layer_name: str = ""
    tile_matrix_sets: list[TileMatrixSet] = field(default_factory=list)
    cds_retrieve_url: Optional[str] = None
    cds_form_url: Optional[str] = None          # CDS form schema JSON URL
    cds_constraints_url: Optional[str] = None   # CDS constraints JSON URL

    # Styling hint
    colormap: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Access-method flags
    # ------------------------------------------------------------------

    @property
    def has_zarr(self) -> bool:
        """True when at least one ARCO Zarr URL is available."""
        return bool(self.zarr_time_chunked or self.zarr_geo_chunked)

    @property
    def has_wmts(self) -> bool:
        """True when a WMTS preview endpoint and layer name are present."""
        return bool(self.wmts_url and self.wmts_layer_name)

    @property
    def has_cds_retrieve(self) -> bool:
        """True when the dataset is available via the CDS download API."""
        return bool(self.cds_retrieve_url)

    @property
    def extraction_supported(self) -> bool:
        """True when data can be extracted to GeoTIFF via either path.

        - ``has_zarr`` → use ``gb.zarr_to_geotiff()`` (fast, synchronous)
        - ``has_cds_retrieve`` → use ``gb.cds_to_geotiff()`` (async, queued)
        """
        return self.has_zarr or self.has_cds_retrieve

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def to_qgis(self) -> Optional[dict]:
        """QgsRasterLayer kwargs dict, or None when no WMTS."""
        if not self.has_wmts:
            return None
        tms = (
            self.tile_matrix_sets[0].identifier
            if self.tile_matrix_sets else "EPSG:3857"
        )
        uri = (
            f"url={self.wmts_url}"
            f"&layers={urllib.parse.quote(self.wmts_layer_name)}"
            f"&styles=default"
            f"&tileMatrixSet={urllib.parse.quote(tms)}"
            f"&format=image/png"
            f"&crs={self.crs}"
        )
        return {"uri": uri, "name": self.title or self.id, "provider": "wms"}

    def to_dict(self) -> dict:
        """JSON-serialisable dictionary."""
        return {
            "id": self.id,
            "title": self.title,
            "service": self.service,
            "abstract": self.abstract,
            "variables": self.variables,
            "keywords": self.keywords,
            "license": self.license,
            "providers": self.providers,
            "thumbnail": self.thumbnail,
            "bbox": list(self.bbox),
            "crs": self.crs,
            "time_range": [
                self.time_range[0].isoformat(),
                self.time_range[1].isoformat(),
            ],
            "time_step": self.time_step,
            "access_methods": {
                "zarr_time_chunked": self.zarr_time_chunked,
                "zarr_geo_chunked":  self.zarr_geo_chunked,
                "wmts": (
                    f"{self.wmts_url}?layer={self.wmts_layer_name}"
                    if self.has_wmts else None
                ),
                "cds_retrieve": self.cds_retrieve_url,
                "cds_form": self.cds_form_url,
                "cds_constraints": self.cds_constraints_url,
            },
            "flags": {
                "has_zarr": self.has_zarr,
                "has_wmts": self.has_wmts,
                "has_cds_retrieve": self.has_cds_retrieve,
                "extraction_supported": self.extraction_supported,
            },
            "colormap": self.colormap,
        }

    def __repr__(self) -> str:
        access = []
        if self.has_zarr:         access.append("zarr")
        if self.has_wmts:         access.append("wmts")
        if self.has_cds_retrieve: access.append("cds")
        return (
            f"LayerDescriptor(id={self.id!r}, service={self.service!r}, "
            f"access={access or ['metadata-only']})"
        )


_ARCO_SNAPSHOT_PATH = (
    Path(__file__).parent.parent / "semantic" / "arco_snapshot.yaml"
)

# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_arco_snapshot() -> dict:
    """Load and cache the ARCO catalogue snapshot.

    Contains all 27 ARCO datasets with Zarr URLs, variables, dimensions,
    optional WMTS endpoints. Generated by ``scripts/refresh_arco_catalogue.py``.
    """
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML required. pip install pyyaml") from exc

    if not _ARCO_SNAPSHOT_PATH.exists():
        logger.warning(
            "ARCO catalogue snapshot not found at %s. "
            "Run scripts/refresh_arco_catalogue.py to generate it.",
            _ARCO_SNAPSHOT_PATH,
        )
        return {}
    with _ARCO_SNAPSHOT_PATH.open(encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    return data.get("datasets", {}) or {}


@lru_cache(maxsize=1)
def _load_cds_snapshot() -> dict:
    """Load and cache the CDS STAC catalogue snapshot (optional).

    Generated by ``scripts/refresh_catalogue.py``. Returns empty dict
    when absent.
    """
    try:
        import yaml
    except ImportError:
        return {}
    if not _SNAPSHOT_PATH.exists():
        return {}
    with _SNAPSHOT_PATH.open(encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    return data.get("datasets", {}) or {}


@lru_cache(maxsize=1)
def _load_overrides() -> dict:
    """Load and cache the slim variable alias overrides."""
    try:
        import yaml
    except ImportError:
        return {}
    if not _OVERRIDES_PATH.exists():
        return {}
    with _OVERRIDES_PATH.open(encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    return data.get("overrides", {}) or {}


# ---------------------------------------------------------------------------
# Building LayerDescriptors
# ---------------------------------------------------------------------------

def _colormap_for_variable(variable: str) -> dict:
    """Return a colormap hint for *variable*, or a neutral default."""
    key = variable.lower().replace("-", "_").replace(" ", "_")
    return dict(_VARIABLE_COLORMAPS.get(key, _DEFAULT_COLORMAP))


def _parse_datetime(value: Optional[str], default: datetime) -> datetime:
    if not value:
        return default
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return default


def _pick_primary_subset(arco_entry: dict) -> Optional[dict]:
    """Choose the primary subset from an ARCO snapshot entry."""
    subsets = arco_entry.get("subsets") or {}
    if not subsets:
        return None
    for preferred in ("sfc", "all", "surface"):
        if preferred in subsets:
            return subsets[preferred]
    return list(subsets.values())[0]


# Names for the pre-composed wind vector layer, which is served alongside the
# u/v components but is not itself listed in any subset's ``variables`` map.
_WIND_LAYER_NAMES = {"wind", "wind10", "wind100", "wind_10m", "wind_100m"}


def _subset_wmts_prefix(subset: dict, dataset_id: str, subset_id: str) -> str:
    """Return the ``{dataset}/{subset}`` layer-name prefix for one subset."""
    raw = (subset.get("wmts") or "").split("?")[0]
    parts = raw.split("/teroWmts/")
    if len(parts) == 2 and parts[1]:
        return parts[1].rstrip("/")
    return f"{dataset_id}/{subset_id}"


def _arco_subset_for_variable(
    dataset_id: str, variable: str
) -> Optional[tuple[str, dict]]:
    """Find the ARCO subset that actually serves *variable*.

    ARCO datasets frequently split their variables across several subsets
    (e.g. ``reanalysis_era5_land`` has eight: ``sfc-soil-temperature``,
    ``sfc-pressure-precipitation``, ...).  The WMTS layer identifier encodes
    the subset, so the correct subset must be resolved per-variable rather
    than reusing the dataset's "primary" subset for everything.

    Returns ``(layer_prefix, subset_dict)`` where ``layer_prefix`` is the
    ``{dataset}/{subset}`` path for the WMTS layer name, or ``None`` when no
    subset lists the variable.
    """
    arco = _load_arco_snapshot()
    arco_id = dataset_id.replace("-", "_")
    entry = arco.get(arco_id)
    if not entry:
        return None
    subsets = entry.get("subsets") or {}

    # 1. Direct hit — a subset whose ``variables`` map contains the name.
    for sub_id, sub in subsets.items():
        if variable in (sub.get("variables") or {}):
            return _subset_wmts_prefix(sub, arco_id, sub_id), sub

    # 2. Pre-composed wind vector layer — resolve to the subset carrying the
    #    matching u/v components.
    if variable.lower() in _WIND_LAYER_NAMES:
        components = ("u100", "v100") if "100" in variable else ("u10", "v10")
        for sub_id, sub in subsets.items():
            svars = sub.get("variables") or {}
            if any(c in svars for c in components):
                return _subset_wmts_prefix(sub, arco_id, sub_id), sub

    return None


def _descriptor_from_arco(dataset_id: str, arco_entry: dict,
                          cds_entry: Optional[dict],
                          global_aliases: dict) -> LayerDescriptor:
    """Build a descriptor from an ARCO snapshot entry."""
    sub = _pick_primary_subset(arco_entry) or {}

    title = arco_entry.get("title", dataset_id)
    abstract = arco_entry.get("description", "")
    service = arco_entry.get("service", "C3S")
    licence = arco_entry.get("license", "unknown")
    providers = arco_entry.get("providers", []) or []
    thumbnail = arco_entry.get("thumbnail", "")

    # Spatial/temporal from subset (more accurate) or dataset level
    bbox_list = sub.get("bbox") or [-180.0, -90.0, 180.0, 90.0]
    if len(bbox_list) >= 4:
        bbox = tuple(float(v) for v in bbox_list[:4])
    else:
        bbox = (-180.0, -90.0, 180.0, 90.0)

    time_start = _parse_datetime(
        sub.get("time_start"), datetime(1940, 1, 1, tzinfo=timezone.utc))
    time_end = _parse_datetime(
        sub.get("time_end"), datetime.now(timezone.utc))

    # Variables from the ARCO snapshot (already in short form)
    var_meta = sub.get("variables") or {}
    variables = sorted(var_meta.keys())

    # Zarr URLs
    zarr = sub.get("zarr") or {}
    zarr_time = zarr.get("timeChunked") or zarr.get("time_chunked")
    zarr_geo = zarr.get("geoChunked") or zarr.get("geo_chunked")

    # WMTS
    wmts_raw = sub.get("wmts") or ""
    wmts_url = ""
    wmts_layer = ""
    if wmts_raw:
        # The STAC asset gives a GetCapabilities URL — extract the base
        base = wmts_raw.split("?")[0]
        # layer name pattern is the path after /teroWmts/
        parts = base.split("/teroWmts/")
        if len(parts) == 2:
            wmts_url = parts[0] + "/teroWmts"
            wmts_layer = parts[1]
        else:
            wmts_url = base

    # CDS retrieve link
    cds_retrieve = None
    cds_form = None
    cds_constraints = None
    if cds_entry:
        links = cds_entry.get("links") or {}
        cds_retrieve = links.get("retrieve")
        cds_form = links.get("form")
        cds_constraints = links.get("constraints")

    # Keywords from CDS snapshot (ARCO snapshot doesn't have them)
    keywords = []
    if cds_entry:
        keywords = cds_entry.get("keywords", []) or []

    # Colormap from the first variable's metadata, or fallback
    colormap = dict(_DEFAULT_COLORMAP)
    if variables:
        first_var = var_meta.get(variables[0], {})
        cm_id = first_var.get("colormap")
        if cm_id:
            colormap = {"palette": cm_id, "unit": first_var.get("unit", "")}
        else:
            colormap = _colormap_for_variable(variables[0])

    return LayerDescriptor(
        id=dataset_id,
        title=title,
        service=service,
        abstract=abstract,
        variables=variables,
        keywords=keywords,
        license=licence,
        providers=[p for p in providers if p],
        thumbnail=thumbnail,
        bbox=bbox,
        crs="EPSG:4326",
        time_range=(time_start, time_end),
        time_step=sub.get("sample_period") or "1h",
        zarr_time_chunked=zarr_time,
        zarr_geo_chunked=zarr_geo,
        wmts_url=wmts_url,
        wmts_layer_name=wmts_layer,
        tile_matrix_sets=[],
        cds_retrieve_url=cds_retrieve,
        cds_form_url=cds_form,
        cds_constraints_url=cds_constraints,
        colormap=colormap,
    )


def _descriptor_from_cds_only(dataset_id: str, cds_entry: dict) -> LayerDescriptor:
    """Build a metadata-only descriptor from a CDS snapshot entry."""
    title = cds_entry.get("title") or dataset_id
    abstract = cds_entry.get("description", "")
    service = cds_entry.get("service", "C3S")
    keywords = cds_entry.get("keywords", []) or []
    licence = cds_entry.get("license", "unknown")
    providers = cds_entry.get("providers", []) or []
    thumbnail = cds_entry.get("thumbnail", "")

    bbox_list = cds_entry.get("spatial_bbox") or [-180.0, -90.0, 180.0, 90.0]
    if len(bbox_list) >= 4 and all(v is not None for v in bbox_list[:4]):
        bbox = tuple(float(v) for v in bbox_list[:4])
    else:
        bbox = (-180.0, -90.0, 180.0, 90.0)

    temporal = cds_entry.get("temporal_interval") or [None, None]
    time_start = _parse_datetime(temporal[0], datetime(1940, 1, 1, tzinfo=timezone.utc))
    time_end = _parse_datetime(
        temporal[1] if len(temporal) > 1 else None,
        datetime.now(timezone.utc))

    links = cds_entry.get("links") or {}
    cds_retrieve = links.get("retrieve")
    cds_form = links.get("form")
    cds_constraints = links.get("constraints")

    return LayerDescriptor(
        id=dataset_id, title=title, service=service, abstract=abstract,
        variables=[], keywords=keywords, license=licence,
        providers=[p for p in providers if p], thumbnail=thumbnail,
        bbox=bbox, crs="EPSG:4326", time_range=(time_start, time_end),
        cds_retrieve_url=cds_retrieve,
        cds_form_url=cds_form,
        cds_constraints_url=cds_constraints,
        colormap=dict(_DEFAULT_COLORMAP),
    )


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def _matches_keyword(d: LayerDescriptor, keyword: str) -> bool:
    needle = keyword.lower()
    haystack = " ".join([d.id, d.title, d.abstract, " ".join(d.keywords)]).lower()
    return needle in haystack


def _matches_variable(d: LayerDescriptor, variable: str) -> bool:
    needle = variable.lower().replace("-", "_")
    return any(needle in v.lower() for v in d.variables)


def _matches_bbox(d: LayerDescriptor, bbox: tuple) -> bool:
    w, s, e, n = bbox
    dw, ds_, de, dn = d.bbox
    return not (de < w or dw > e or dn < s or ds_ > n)


def _matches_time(d: LayerDescriptor, time_after: datetime) -> bool:
    if time_after.tzinfo is None:
        time_after = time_after.replace(tzinfo=timezone.utc)
    end = d.time_range[1]
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end >= time_after


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover(
    keyword: Optional[str] = None,
    services: Optional[list[str]] = None,
    variable: Optional[str] = None,
    bbox: Optional[tuple[float, float, float, float]] = None,
    time_after: Optional[datetime] = None,
    extraction_only: bool = False,
    arco_only: bool = False,
) -> list[LayerDescriptor]:
    """Return Copernicus datasets matching the given filters.

    Reads the bundled STAC snapshot and ARCO overrides file — no
    network call is made.  When the snapshot is missing, only datasets
    present in the overrides file are returned.

    Parameters
    ----------
    keyword : str, optional
        Case-insensitive substring applied to id, title, abstract, and
        keywords.
    services : list[str], optional
        Restrict to ``'C3S'``, ``'CAMS'``, and/or ``'CEMS'``.
    variable : str, optional
        Filter to datasets whose variable list contains this name.
    bbox : tuple, optional
        ``(west, south, east, north)`` in WGS-84.  Returns only
        descriptors whose spatial extent intersects this region.
    time_after : datetime, optional
        Return only datasets with data ending after this date.
    extraction_only : bool
        When True, return only datasets that support extraction via
        either path: ``has_zarr`` (ARCO) or ``has_cds_retrieve`` (CDS API).
        As of May 2026 this is all 136 datasets.
    arco_only : bool
        When True, return only datasets available in the ARCO Zarr store
        (``has_zarr=True``). These support fast synchronous extraction via
        ``gb.zarr_to_geotiff()``. Currently 26 datasets.

    Returns
    -------
    list[LayerDescriptor]
        Sorted alphabetically by id.

    Examples
    --------
    >>> import geobridge as gb
    >>> # All ARCO datasets (fast extraction)
    >>> for ds in gb.discover(arco_only=True):
    ...     print(ds.id)
    >>> # All datasets that can be extracted (ARCO + CDS API)
    >>> for ds in gb.discover(extraction_only=True):
    ...     print(ds.id, "ARCO" if ds.has_zarr else "CDS API")
    """
    arco = _load_arco_snapshot()
    cds = _load_cds_snapshot()
    aliases = (_load_overrides() or {}).get("variable_aliases", {})

    # ARCO ids use underscores; CDS ids use hyphens.  Build a unified
    # set using the underscore convention for ARCO and hyphens for CDS.
    all_ids: dict[str, str] = {}  # display_id → source
    for ds_id in arco:
        all_ids[ds_id] = "arco"
    for ds_id in cds:
        # Only add CDS-only entries (not already covered by ARCO)
        arco_form = ds_id.replace("-", "_")
        if arco_form not in arco:
            all_ids[ds_id] = "cds"

    valid_services = {"C3S", "CAMS", "CEMS"}
    if services:
        unknown = set(services) - valid_services
        if unknown:
            raise ValueError(
                f"Unknown service(s): {unknown}. Valid: {sorted(valid_services)}"
            )
        services_set = set(services)
    else:
        services_set = valid_services

    results: list[LayerDescriptor] = []
    for ds_id, source in all_ids.items():
        if source == "arco":
            cds_id = ds_id.replace("_", "-")
            desc = _descriptor_from_arco(
                ds_id, arco[ds_id], cds.get(cds_id), aliases)
        else:
            desc = _descriptor_from_cds_only(ds_id, cds[ds_id])

        if desc.service not in services_set:              continue
        if keyword  and not _matches_keyword(desc, keyword):  continue
        if variable and not _matches_variable(desc, variable): continue
        if bbox     and not _matches_bbox(desc, bbox):         continue
        if time_after and not _matches_time(desc, time_after): continue
        if extraction_only and not desc.extraction_supported:  continue
        if arco_only and not desc.has_zarr:                    continue
        results.append(desc)

    results.sort(key=lambda d: d.id)
    logger.info("discover() returned %d dataset(s)", len(results))
    return results


def discover_one(
    dataset_id: str,
    **kwargs,
) -> Optional[LayerDescriptor]:
    """Return a single LayerDescriptor by id, or None if not found.

    Accepts both CDS-style (hyphenated) and ARCO-style (underscored) ids.
    Without kwargs, does a fast direct lookup.  With kwargs, routes
    through :func:`discover` and returns the first matching result.

    Examples
    --------
    >>> ds = gb.discover_one("reanalysis-era5-single-levels")
    >>> print(ds.zarr_time_chunked)
    """
    if kwargs:
        for ds in discover(**kwargs):
            if dataset_id.lower() in ds.id.lower():
                return ds
        return None

    arco = _load_arco_snapshot()
    cds = _load_cds_snapshot()
    aliases = (_load_overrides() or {}).get("variable_aliases", {})

    # Try ARCO first (underscore form)
    arco_id = dataset_id.replace("-", "_")
    if arco_id in arco:
        cds_id = arco_id.replace("_", "-")
        return _descriptor_from_arco(arco_id, arco[arco_id], cds.get(cds_id), aliases)

    # Try CDS (hyphen form)
    if dataset_id in cds:
        return _descriptor_from_cds_only(dataset_id, cds[dataset_id])

    return None
