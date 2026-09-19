# GeoBridge Form Module — Reference Notes

Source: `geobridge/modules/form.py`. Last reviewed against commit `c0640a6`.
See also [DISCOVERY.md](DISCOVERY.md) (the snapshot this module falls back
to) and [CDS_DOWNLOAD](../geobridge/modules/cds_download.py) (the module
`validate_request` is meant to protect — form.py itself is not documented
there yet).

## 1. What it does

`form.py` fetches and parses the **CDS download form schema** for a
dataset — the same JSON the CDS web portal's request-builder UI reads to
render its form (variable pickers, year/month/day widgets, pressure-level
lists, etc.) — plus the accompanying **constraints JSON** (which
combinations of those values are actually valid together). Together they
let GeoBridge validate a `cds_to_geotiff()` request *before* submitting it,
instead of discovering a bad parameter combination only after a job fails
on ECMWF's servers.

Unlike `discover.py`, **no form data is bundled** with the package — it is
always fetched live over HTTP from the ECMWF object store (which serves it
with a 1-hour cache header). This is deliberate: forms and constraints
change more often and more consequentially than the dataset catalogue
itself (see §2), so baking them into a version-pinned snapshot would go
stale in ways that silently produce wrong validation results.

## 2. Live-first, snapshot-fallback URL resolution

Both the form URL and the constraints URL are resolved by
`_get_form_url` / `_get_constraints_url`, which follow the same two-step
pattern:

1. **Live CDS catalogue API** — `_fetch_live_links(cds_id)` calls
   `https://cds.climate.copernicus.eu/api/catalogue/v1/collections/{cds_id}`
   and reads the `form`/`constraints` link relations from the response.
   This is authoritative and always current.
2. **Bundled CDS snapshot fallback** — `_snapshot_link()` reads
   `geobridge/semantic/cds_snapshot.yaml` (the same file `discover.py`
   loads via `_load_cds_snapshot`) for a pre-recorded link, used only when
   the live lookup fails (offline, CDS unreachable, rate-limited).

**Why live-first matters here specifically**: the CDS catalogue rotates
form/constraints URLs to a new content-hashed filename whenever a
dataset's form fields or valid-combination rules change — but the *old*
hashed URL keeps serving its (now stale) content instead of 404ing. A URL
frozen into a snapshot at release time can therefore silently point at
outdated rules indefinitely. Resolving live is the only way to be sure the
URL in use is current. `_fetch_live_links` is `@lru_cache`d per dataset id
per process, so this costs exactly one extra request per dataset per
session — not one per call — and is cleared along with everything else by
`clear_form_cache()`.

## 3. Parsing the form (`_parse_widget`)

The CDS form JSON is a flat list of widget definitions. Each becomes a
`FormWidget(name, label, widget_type, values, required, details)`. The
tricky part is that CDS encodes a widget's allowed values in **two
different shapes** depending on `widget_type`, and `_parse_widget`
normalises both into one flat `[{value, label}]` list (first-seen order,
deduplicated):

- **Flat** (`StringListWidget` / `StringChoiceWidget`) — a top-level
  `details.values` list plus a separate `details.labels` map.
- **Grouped** (`StringListArrayWidget`) — values split across accordion
  groups under `details.groups` (or `details.accordionGroups`), each
  group carrying its own `values`/`labels`.

Widgets with no enumerable values at all (`FreeformInputWidget`,
`DateRangeWidget`, `GeographicExtentWidget`) simply end up with an empty
`values` list — `validate_request` treats those as unconstrained free-form
parameters and skips the allowed-values check for them (see §5).

## 4. Data classes

### `FormWidget`
One parameter in the form. Convenience accessors:
`.value_list` (flat list of raw values), `.label_map` (`{value: label}`).

### `FormSchema`
The full parsed form for one dataset: `dataset_id`, `widgets`, and the
`raw` JSON. Convenience accessors for the parameters GeoBridge cares about
most: `.get_widget(name)`, `.variable_widget`, `.product_type_widget`,
`.year_widget`, `.pressure_level_widget`, plus flattened helpers
`.parameter_names()`, `.variables()`, `.years()`, `.product_types()`,
`.pressure_levels()`.

## 5. Public API

### `fetch_form(dataset_id, timeout=20) -> Optional[FormSchema]`
Resolves the form URL (§2), fetches and parses it, caches the result
in-memory keyed by the hyphenated (CDS-style) dataset id (`_FORM_CACHE`).
Returns `None` — logged as a warning, never raised — if no form URL can be
found or the fetch/parse fails. Accepts both hyphenated and underscored
dataset ids.

### `fetch_constraints(dataset_id, timeout=20) -> list[dict]`
Resolves the constraints URL, fetches the JSON, caches in
`_CONSTRAINTS_CACHE`. Each entry in the returned list is a
`{parameter_name: [valid_values]}` dict — a combination is valid only when
**all** parameters in that one dict entry are satisfied jointly (it's a
list of valid joint-combination "cells", not per-parameter allowed
values). Returns `[]` on any failure.

### `valid_variables_for_product_type(dataset_id, product_type) -> list[str]`
Filters `fetch_constraints()` down to the variables valid for one specific
`product_type` — e.g. "show only variables available in the monthly
averaged reanalysis, not all ERA5 variables." Returns `[]` if constraints
are unavailable.

### `validate_request(dataset_id, request) -> list[str]`
The main reason this module exists. Runs two independent checks against a
CDS request dict (the same shape passed to `gb.cds_to_geotiff()`) and
returns a list of human-readable error strings (empty list = looks valid):

1. **Allowed values** (via `fetch_form`) — every submitted value for every
   parameter must appear in that parameter's form enum. Parameters not
   present in the form, or with no enumerable values (free-form widgets),
   are skipped rather than flagged. `data_format` is always skipped — it's
   a GeoBridge/API-level parameter, not a form field. If no form schema is
   available at all, this check is skipped entirely (logged as a warning)
   rather than failing closed.
2. **Valid combinations** (via `fetch_constraints`) — the request's values
   must jointly satisfy at least one constraint entry (set-intersection
   per parameter, so multi-value request fields like
   `"variable": ["a", "b"]` pass as long as at least one submitted value
   per parameter overlaps the constraint cell). If the combination matches
   no constraint entry, one error is added pointing at the dataset's CDS
   portal page for the allowed combinations. Skipped if no constraints URL
   is available for the dataset.

```python
from geobridge.modules.form import validate_request

errors = validate_request("derived-utci-historical", {
    "product_type": ["consolidated_dataset"],
    "variable": ["universal_thermal_climate_index"],
    "year": ["2025"], "month": ["01"], "day": ["01"],
    "data_format": "grib",
})
if errors:
    for e in errors:
        print(e)
```

### `clear_form_cache()`
Clears all three in-memory caches (`_FORM_CACHE`, `_CONSTRAINTS_CACHE`,
and `_fetch_live_links`'s `lru_cache`). Mainly useful in tests, or in a
long-running process where a dataset's form may have changed mid-session.

## 6. Authentication and network behaviour

**No `gb.authenticate()` call is required** — the CDS catalogue, form, and
constraints endpoints used here are public metadata, unlike the actual
data-retrieval paths in `extract.py`/`cds_download.py`. Every HTTP request
goes out with a plain `Accept: application/json` + `User-Agent:
geobridge/0.1` header (`_fetch_json`) — no auth header at all.

## 7. Relationship to the other modules

- Reads `geobridge/semantic/cds_snapshot.yaml` via
  `discover._load_cds_snapshot` for its fallback links — the only coupling
  to `discover.py`.
- Its natural caller is `modules/cds_download.py`: validate a request with
  `form.validate_request()` before calling `gb.cds_to_geotiff()`, to catch
  a bad parameter combination locally instead of waiting on a queued CDS
  job to fail.
