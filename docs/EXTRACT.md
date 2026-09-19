# GeoBridge Extract Module — Reference Notes

Source: `geobridge/modules/extract.py`. Last reviewed against commit `c0640a6`.
See also [AUTHENTICATION.md](AUTHENTICATION.md) (this path requires auth) and
[DISCOVERY.md](DISCOVERY.md) (the catalogue this module reads from).

## 1. What it does

`extract.py` is the **ARCO Zarr extraction path**: it opens a Copernicus
ARCO (Analysis-Ready, Cloud-Optimized) Zarr store directly over HTTP,
subsets it spatially/temporally, optionally aggregates over time, and
writes the result as a (Cloud Optimized) GeoTIFF. This is the fast,
**synchronous** extraction path — contrast with `modules/cds_download.py`,
which submits an async job to the CDS "retrieve" API for datasets not
available as ARCO Zarr.

Requires the optional `[zarr]` extras (`pip install "geobridge[zarr]"`) —
`xarray`, `rioxarray`, `numpy` are imported lazily inside `_require_xarray()`
so the base package doesn't force-install heavy geospatial dependencies.

Designed against the live ECMWF ARCO Data Lake at
`arco.datastores.ecmwf.int` (verified 2026-05-05).

## 2. Two Zarr "flavours" — time_chunked vs geo_chunked

Each ARCO dataset/subset can ship up to two physical Zarr layouts of the
same data:

| Flavour | Chunked along | Best for |
|---|---|---|
| `time_chunked` | time axis | spatial maps over short periods (typical GIS use case) |
| `geo_chunked` | spatial axes | long time series at a single point |

`_pick_chunking(entry, time_range, bbox)` auto-selects when only one
flavour is available for the dataset, it's the obvious choice. Otherwise a
heuristic applies: **tiny bbox (< 1 deg²) + long time range (> 90 days,**
`_GEO_CHUNK_THRESHOLD_DAYS`**) → `geo_chunked`; otherwise `time_chunked`.**
Callers can override via `zarr_to_geotiff(..., chunking="geo_chunked")`.

## 3. Catalogue lookup — independent of `discover.py`

`extract.py` does **not** reuse `discover.LayerDescriptor`. It has its own
loader, `_get_dataset_entry(dataset_id, subset=None)`, because extraction
needs a richer per-subset shape than the discovery layer exposes:
dimensions, native grid resolution, latitude/longitude conventions, and a
resolved variable-alias map. It reads the same three YAML snapshots as
`discover.py` (`arco_snapshot.yaml`, `arco_overrides.yaml`,
`cds_snapshot.yaml` — attached under `entry["_cds"]` if present) but keeps
its own `@lru_cache`-backed loaders.

Key steps inside `_get_dataset_entry`:

1. Normalise the id: hyphens → underscores (`reanalysis-era5-...` → `reanalysis_era5_...`).
2. If missing from the ARCO snapshot, fall back to `arco_overrides.yaml`
   entries that carry their own inline `zarr` block; otherwise raise
   `ExtractionError` listing the first 10 available dataset ids and
   pointing at `scripts/refresh_arco_catalogue.py`.
3. Pick a subset via `_pick_default_subset` — same preference order as
   discovery's `_pick_primary_subset` (`sfc` → `all` → `surface` → first
   alphabetically) unless the caller passed `subset=`.
4. Normalise Zarr key casing: ECMWF's STAC uses `timeChunked`/`geoChunked`;
   `_normalise_zarr_keys` converts to `time_chunked`/`geo_chunked`.
5. Derive `native_resolution_deg` from the latitude dimension's `step`, and
   `coordinate_order` (`lat_ascending`/`lat_descending`) and
   `longitude_convention` (`minus180_to_180`/`zero_to_360`) from the
   dimension extents — these drive the subsetting logic in step 6 below.
6. Merge variable aliases: top-level `arco_overrides.yaml` aliases plus any
   dataset-specific overrides (dataset-specific wins on conflict).

Variable names accepted are either CDS long form (`2m_temperature`) or ARCO
short form (`t2m`) — `_resolve_variable_name` translates via the alias map;
an unrecognised name is passed through unchanged and left to raise a clear
`KeyError` from xarray later rather than being silently swallowed.

## 4. Opening the store — where authentication plugs in

`_open_arco_zarr(zarr_url)`:

```python
if not is_authenticated():
    raise ExtractionError(...)          # explicit pre-check, friendly message
storage_options = {"headers": auth_header()}   # Authorization: Bearer <key>
xr.open_zarr(zarr_url, consolidated=True, storage_options=storage_options, chunks="auto")
```

This is the one place `extract.py` touches `geobridge.auth`
(`auth_header`, `is_authenticated` — see [AUTHENTICATION.md](AUTHENTICATION.md)
§4 for the full picture of why ARCO uses `Bearer` while the CDS job API
uses `PRIVATE-TOKEN`). Failures here (invalid/expired key, licence not
accepted, rate limiting, stale Zarr URL) are caught and re-raised as
`ExtractionError` with a checklist of the four likely causes — the
underlying xarray/fsspec exception is chained (`from exc`) so the root
cause is still visible.

`chunks="auto"` means xarray/dask picks chunk sizes for lazy, out-of-core
reads — nothing is pulled over the wire until subsetting/writing forces
computation.

## 5. Subsetting (`_select_subset`)

Given the opened `xr.Dataset`, the resolved short variable name, a bbox,
and a time range:

- Validates the variable exists in `ds.data_vars`; on miss, raises
  `ExtractionError` showing up to the first 20 available variable names.
- Resolves coordinate names dynamically since they vary by dataset:
  longitude ∈ {`longitude`, `lon`, `x`}, latitude ∈ {`latitude`, `lat`,
  `y`}, time ∈ {`time`, `valid_time`, `t`} — via `_coord_name`.
- Handles the **0–360 vs -180–180 longitude convention** automatically
  using `entry["longitude_convention"]` derived earlier: if the dataset
  uses 0–360, negative bbox longitudes are shifted by +360 before slicing.
- Handles **latitude direction** (ascending vs descending grids) by
  inspecting the actual coordinate values at runtime and building the
  correct `slice()` order — not just trusting the catalogue metadata.
- After slicing, if any dimension size is `0` the request produced an
  empty array — raises `ExtractionError` explaining the likely cause
  (bbox smaller than one grid cell, or time range outside dataset
  coverage), including the dataset's native grid spacing in the message.
- Returns `(da, coord_names)` where `coord_names` records which axis names
  were actually used, for later steps.

## 6. Aggregation, CRS, reprojection, write

- **`_aggregate(da, aggregation, time_name)`** — optional temporal resample.
  Supported values: `raw` (no-op, default), `daily_mean/max/min`,
  `monthly_mean/max/min`, `annual_mean/max`. Implemented as
  `da.resample({time_name: freq}).{op}()` with pandas offset aliases (`D`,
  `MS`, `YS`). Unknown aggregation name → `ExtractionError` listing valid
  options. No-op if there's no time dimension at all.
- **`_ensure_crs(da, lon_name, lat_name)`** — renames coordinates to `x`/`y`
  (what rioxarray expects), forces the y-axis to be north-up/descending
  (rioxarray needs this to compute a valid affine transform — otherwise
  GDAL emits a `NotGeoreferencedWarning`), sets spatial dims, and writes
  `EPSG:4326` as the CRS if none is already set. Also clears any stale
  `transform` in `.encoding` so rioxarray recomputes it fresh from the
  coordinate arrays instead of trusting a cached identity transform.
- **Optional reprojection** — if `target_crs` is given and isn't
  `EPSG:4326`, calls `da.rio.reproject(target_crs)`; failures become
  `ExtractionError`.
- **`_band_descriptions`** — builds one descriptive band name per output
  band as `{dataset}_{variable}_{ISO-timestamp}` (or just
  `{dataset}_{variable}` if there's no time axis), written into the GeoTIFF
  as GDAL per-band descriptions (`long_name` attr on the DataArray) so
  downstream GIS tools show meaningful band labels instead of "Band 1".
- **`_write_geotiff`** — `cog=True` (default) writes `driver="COG"` with
  `DEFLATE` compression and 512px blocks; `cog=False` writes a plain
  tiled `GTiff`. Creates parent directories as needed.

## 7. Public API

### `list_datasets() -> list[str]`
All dataset ids in the ARCO snapshot (falls back to `arco_overrides.yaml`
keys if the snapshot file is missing). As of the bundled snapshot: 27
ARCO-wired datasets, all auto-discovered — no per-dataset code changes
needed when the snapshot is refreshed.

### `list_variables(dataset_id, subset=None) -> list[str]`
ARCO short-form variable names (e.g. `t2m`, `pm2p5`, `utci`) for a dataset
(or its named subset). Either short or long names may be passed to
`zarr_to_geotiff`.

### `zarr_to_geotiff(dataset, variable, bbox, time_range, aggregation="raw", output_path=None, cog=True, target_crs=None, chunking=None) -> Path`

The main entry point — runs the full pipeline described above in order:
catalogue lookup → pick chunking/Zarr URL → open store (auth-checked) →
subset → aggregate → attach CRS → optional reproject → write. Defaults
`output_path` to `./{dataset}_{variable}_{aggregation}.tif` in the current
directory when not given. Raises `ExtractionError` for any failure at any
stage (dataset/variable not found, auth missing, empty subset, write
failure) — always with an actionable message.

```python
import geobridge as gb

gb.authenticate()
tif = gb.zarr_to_geotiff(
    dataset="reanalysis-era5-single-levels",
    variable="2m_temperature",
    bbox=(23.0, 37.5, 24.5, 38.5),
    time_range=("2023-07-01", "2023-07-31"),
    aggregation="monthly_mean",
    cog=True,
)
```

## 8. Error handling summary

All errors surfaced by this module are `ExtractionError` (a `RuntimeError`
subclass) regardless of the underlying cause — dataset/subset not found,
variable not in the store, not authenticated, empty subset after slicing,
reprojection failure, or write failure. Each message is written to be
actionable on its own (lists valid options, points at the relevant script
or config file) rather than requiring the user to dig into the traceback.
