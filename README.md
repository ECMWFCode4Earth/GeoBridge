# GeoBridge

**Python library that bridges Copernicus Data Stores into mainstream GIS workflows.**

GeoBridge removes the technical friction GIS users face when working with
Copernicus climate, atmosphere, and emergency data. It resolves dataset
discovery, authentication, and access-method differences into a single
ergonomic API that produces analysis-ready Cloud Optimized GeoTIFFs.

The library is the foundation for the project's QGIS plugin.

---

## What it does

- **Discovery.** `gb.discover()` lists every dataset in the Copernicus
  catalogue with filters for keyword, service, variable, bounding box, and
  time range — all from a locally bundled snapshot, no network needed.
- **Authentication.** A single `gb.authenticate()` call handles credential
  resolution for the new ARCO bearer-token model.
- **Extraction — ARCO path.** `gb.zarr_to_geotiff()` pulls a spatial / temporal
  subset from the ARCO Zarr Data Lake and writes it as a Cloud Optimized
  GeoTIFF, ready for QGIS or ArcGIS.
- **Extraction — CDS API path.** `gb.cds_to_geotiff()` submits a download job
  through the standard CDS API for datasets that are not yet in the ARCO lake,
  converts the result to GeoTIFF, and streams it to disk.
- **WMTS.** `gb.wmts_layer()` returns a ready-to-use WMTS layer object for
  live tile streaming inside QGIS or Leaflet.
- **Form schema.** `gb.fetch_form()` and `gb.fetch_constraints()` retrieve the
  server-side parameter form for any dataset so your UI can build validated
  request widgets. `gb.valid_variables_for_product_type()` filters the variable
  list to what the selected product type actually supports.
- **Styling.** `gb.to_qgis_style()` generate
  calibrated colour ramps for known Copernicus variables.
- **Fusion.** `gb.fuse()` co-registers multiple layers onto a common grid
  for joint analysis (e.g. heat + air quality).
- **Semantics.** `gb.semantic_search()` resolves user themes like
  "urban heat island" or "wildfire risk" into concrete dataset and
  workflow recommendations. `gb.list_themes()` and `gb.list_use_cases()`
  enumerate the built-in vocabulary.

---

## How to run it

### Prerequisites

- **Python 3.10 or newer.** GeoBridge does not support older Python.
- **A free Copernicus account.** Register at
  https://cds.climate.copernicus.eu and copy your personal access token
  from your profile page.
- **macOS, Linux, or Windows with WSL2.** Native Windows may work but is
  not tested.

### Step 1 — Create a clean environment

A separate environment avoids dependency conflicts with anything else you
have installed. With Conda (recommended because some geospatial libraries
need compiled C bindings that pip alone struggles with):

```bash
conda create -n geobridge python=3.11 -y
conda activate geobridge
```

Or with `venv`:

```bash
python3.11 -m venv ~/.venvs/geobridge
source ~/.venvs/geobridge/bin/activate
```

### Step 2 — Install the geospatial stack

If you used Conda, install the heavy native dependencies through
conda-forge first:

```bash
conda install -c conda-forge rasterio rioxarray zarr fsspec httpio dask -y
```

This avoids the most common build failures (GDAL, PROJ, libtiff).

### Step 3 — Clone and install GeoBridge

```bash
git clone https://github.com/YOUR_USERNAME/geobridge.git
cd geobridge
pip install -e ".[zarr]"
```

`-e` installs in editable mode so you can pull updates without
reinstalling. The `[zarr]` extra adds the optional dependencies needed
for `zarr_to_geotiff()`. Use `.[full]` to also get OWSLib (WMTS) and
cfgrib (GRIB support).

### Step 4 — Configure your credentials

Create `~/.cdsapirc` with your personal access token:

```
key: YOUR-CDS-API-KEY-HERE
```

Then protect the file so other users on the machine cannot read it:

```bash
chmod 600 ~/.cdsapirc
```

Alternatively, export your key as an environment variable instead of
writing it to a file:

```bash
export CDS_API_KEY=YOUR-CDS-API-KEY-HERE
```

### Step 5 — Verify the installation

Run the bundled smoke test. It exercises every module without making
network calls:

```bash
PYTHONPATH=. python smoke_test.py
```

Expected: nine stages all pass, ending with `ALL SMOKE TESTS PASSED`.

### Step 6 — Run the Athens demo

This is the end-to-end demonstration. It extracts ERA5 temperature for
Athens summer 2023, writes a Cloud Optimized GeoTIFF, and generates a
matching QGIS style:

```bash
python examples/athens_urban_heat.py
```

Expected runtime is roughly one minute on a reasonable broadband
connection. The output appears in `./athens_outputs/`. Open the `.tif` in
QGIS, load the `.qml` style alongside it, and you should see Athens
temperatures rendered with a calibrated colour ramp.

---

## Repository layout

```
geobridge/                              ← repository root
├── README.md                           ← this file
├── LICENSE                             ← MIT
├── pyproject.toml                      ← installable Python package
├── smoke_test.py                       ← offline smoke test (9 stages)
│
├── geobridge/                          ← Python package
│   ├── __init__.py                     ← public API exports
│   ├── auth.py                         ← bearer-token authentication
│   ├── modules/
│   │   ├── discover.py                 ← catalogue discovery
│   │   ├── extract.py                  ← ARCO Zarr → GeoTIFF
│   │   ├── cds_download.py             ← CDS API → GeoTIFF (non-ARCO datasets)
│   │   ├── wmts.py                     ← WMTS layer
│   │   ├── form.py                     ← dataset form schema & constraints
│   │   ├── style.py                    ← QGIS QML export
│   │   └── fuse.py                     ← multi-layer co-registration
│   └── semantic/
│       ├── engine.py                   ← rule-based query resolver
│       ├── vocabulary.yaml             ← themes and use cases
│       ├── arco_overrides.yaml         ← Zarr URLs and variable aliases
│       ├── arco_snapshot.yaml          ← ARCO catalogue snapshot (generated)
│       └── cds_snapshot.yaml           ← STAC catalogue snapshot (generated)
│
├── scripts/
│   ├── refresh_catalogue.py            ← maintainer-side CDS STAC refresh
│   └── refresh_arco_catalogue.py       ← maintainer-side ARCO snapshot refresh
│
├── examples/
│   ├── athens_urban_heat.py            ← end-to-end ERA5 demo
│   ├── example_utci.py                 ← UTCI thermal comfort demo
│   ├── examplepm2.5.py                 ← CAMS PM2.5 air quality demo
│   └── ...                             ← additional live-test scripts
│
└── tests/
    ├── test_auth.py
    └── unit/                           ← pytest unit tests
        ├── test_discover.py
        ├── test_auth.py
        ├── test_extract.py
        ├── test_style.py
        ├── test_semantic.py
        └── test_wmts.py
```

---

## Maintainer workflow

### Refreshing catalogue snapshots

The `cds_snapshot.yaml` and `arco_snapshot.yaml` files are regenerated
periodically from the live ECMWF catalogues. End users never run these
scripts; they get the snapshots bundled with whatever GeoBridge version
they install.

To refresh the CDS STAC snapshot (maintainers only):

```bash
python scripts/refresh_catalogue.py --limit 5 --output /tmp/test.yaml   # quick test
python scripts/refresh_catalogue.py                                      # full run
git diff geobridge/semantic/cds_snapshot.yaml
git add geobridge/semantic/cds_snapshot.yaml
git commit -m "Refresh CDS catalogue snapshot"
```

To refresh the ARCO snapshot:

```bash
python scripts/refresh_arco_catalogue.py
git add geobridge/semantic/arco_snapshot.yaml
git commit -m "Refresh ARCO catalogue snapshot"
```

### Adding a new ARCO dataset

1. Visit the dataset page on https://cds.climate.copernicus.eu and open
   the "Analysis ready data" tab.
2. Copy the Zarr URLs (typically there are two: `time_chunked` and
   `geo_chunked`).
3. Verify each URL responds with a 200 status:

   ```bash
   curl -I -H "Authorization: Bearer $CDS_API_KEY" "https://.../.zmetadata"
   ```

4. Add an entry to `geobridge/semantic/arco_overrides.yaml` following the
   schema of existing entries.
5. Add the variable aliases (CDS long-form name → ARCO short-form name)
   by inspecting the Zarr store with `xarray.open_zarr()` and listing
   `ds.data_vars`.

---

## License

MIT. See `LICENSE`.

---

## Acknowledgements
