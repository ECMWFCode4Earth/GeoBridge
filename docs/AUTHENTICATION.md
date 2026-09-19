# GeoBridge Authentication — Reference Notes

Source: `geobridge/auth.py`, consumed by `geobridge/modules/extract.py` and
`geobridge/modules/cds_download.py`. Last reviewed against commit `c0640a6`.

## 1. What GeoBridge authenticates against

GeoBridge does not have its own user accounts or auth server. It is a client
library for the **Copernicus Climate Data Store (CDS)**. The only credential
in play is your **CDS personal API key** (a token you get from your CDS
profile page). GeoBridge just resolves that one key and attaches it to
outgoing HTTP requests in whatever shape each downstream Copernicus service
expects.

There is no OAuth flow, no login step, no refresh tokens — the key is a
long-lived bearer credential you paste in once.

## 2. The core object: `_AuthSession`

`geobridge/auth.py` defines a tiny module-level singleton:

```python
_session: Optional["_AuthSession"] = None
```

Calling `gb.authenticate()` builds one `_AuthSession(api_key, api_url)` and
stores it in that module global. This means:

- Authentication is **process-wide**, not per-object. You call
  `gb.authenticate()` once at the top of a script/notebook, and every
  GeoBridge function called afterwards can see it.
- It is **not thread-safe** by design — it's a single global, so don't
  authenticate as two different users in the same process.
- There's no persistence between runs beyond whatever `authenticate()` reads
  credentials from (see §3) — each new Python process must call
  `authenticate()` again.

## 3. Credential resolution order

`authenticate(key=None, url=_DEFAULT_API_URL)` resolves the key via
`_resolve_key()`, checked in this priority order:

1. **Explicit `key=` argument** — `gb.authenticate(key="xxxx-xxxx")`
2. **Environment variable `CDS_API_KEY`**
3. **`~/.cdsapirc`** — the standard CDS client config file, parsed for a
   `key:` or `key=` line (quotes are stripped, both `:` and `=` separators
   supported, e.g. `key: abcd1234` or `key=abcd1234`)

If none of the three produce a key, `authenticate()` raises
`AuthenticationError` with setup instructions pointing to
https://cds.climate.copernicus.eu/profile.

This mirrors the standard `cdsapi` Python client's own resolution logic, so
if you already have `~/.cdsapirc` set up for the official `cdsapi` package,
GeoBridge picks it up automatically with zero extra config.

## 4. Two different header schemes for two different Copernicus APIs

This is the part most worth remembering — **the same API key is sent in two
different HTTP header formats**, depending on which backend GeoBridge is
talking to:

| Access path | Module | Header format | Where |
|---|---|---|---|
| ARCO Zarr (cloud-optimized, direct array read) | `modules/extract.py` | `Authorization: Bearer <key>` | `auth.auth_header()` → `_AuthSession.auth_header` |
| CDS "retrieve" API (OGC API Processes — job submit/poll/download) | `modules/cds_download.py` | `PRIVATE-TOKEN: <key>` | local `_auth_headers()` helper, calls `auth.get_token()` |

- `auth_header()` returns `{"Authorization": f"Bearer {api_key}"}` — used as
  `storage_options={"headers": ...}` when GeoBridge calls
  `xarray.open_zarr(...)` directly against Copernicus's ARCO Zarr store.
- `get_token()` just returns the raw key string — `cds_download.py` wraps it
  itself into `PRIVATE-TOKEN` because that's what the newer CDS "OGC API
  Processes" job-submission endpoint expects (job POST → poll status →
  download result file, for datasets not available as ARCO Zarr).

So: one key, two wire formats, chosen automatically depending on whether
GeoBridge is reading Zarr directly or submitting a CDS retrieval job.

**WMTS tile access (`modules/wmts.py`) requires no authentication at all** —
it's documented as a public endpoint (`https://wmts.datastores.ecmwf.int/teroWmts`).
So `wmts_layer()` and related map-tile functions work without calling
`gb.authenticate()` first.

## 5. Public API surface (`geobridge/auth.py`)

| Function | Purpose |
|---|---|
| `authenticate(key=None, url=...)` | Resolve credentials, create the session singleton. Call once. |
| `is_authenticated()` | `True` if `authenticate()` succeeded and the session exists. |
| `get_token()` | Raw API key string (the bearer token). Raises `AuthenticationError` if not authenticated. |
| `auth_header()` | `{"Authorization": "Bearer <key>"}` dict, ready for `storage_options["headers"]`. |
| `AuthenticationError` | Raised for missing/invalid credentials — subclass of `RuntimeError`. |

All are re-exported at the top level, so `import geobridge as gb` gives you
`gb.authenticate`, `gb.auth_header`, `gb.get_token`, `gb.is_authenticated`,
`gb.AuthenticationError` directly (see `geobridge/__init__.py`).

## 6. Where authentication is actually enforced

- `extract.py::_open_arco_zarr()` explicitly checks `is_authenticated()`
  before opening a Zarr store and raises `ExtractionError` (not
  `AuthenticationError`) with a friendly message if you forgot to call
  `authenticate()`.
- `cds_download.py` doesn't pre-check; it just calls `get_token()` inside
  `_auth_headers()`, which raises `AuthenticationError` from
  `_require_session()` if no session exists.
- On CDS API **HTTP 401**, `cds_download.py` translates it into a
  `CdsApiError` with actionable guidance — most 401s from CDS are actually
  "you haven't accepted the dataset's licence on the CDS portal," not a bad
  key, so the error message walks the user through accepting terms at
  `https://cds.climate.copernicus.eu/datasets/{dataset}` before suggesting
  they check the key itself.

## 7. Typical usage

```python
import geobridge as gb

gb.authenticate()                      # reads ~/.cdsapirc, or:
gb.authenticate(key="xxxxxxxx-xxxx")   # explicit key, or:
# export CDS_API_KEY=xxxxxxxx-xxxx     # env var, picked up automatically

gb.is_authenticated()   # -> True

# ARCO Zarr path (needs auth)
tif = gb.zarr_to_geotiff(dataset=..., variable=..., bbox=..., time_range=...)

# CDS retrieve/job path (needs auth, different header under the hood)
tif = gb.cds_to_geotiff(dataset=..., request={...}, bbox=..., output_path=...)

# WMTS tiles (no auth needed)
layer = gb.wmts_layer(dataset=..., variable=..., datetime=...)
```

## 8. Getting a key

Personal CDS API key: https://cds.climate.copernicus.eu/profile
CDS API setup docs: https://cds.climate.copernicus.eu/how-to-api

## 9. Test coverage

`tests/unit/test_auth.py` covers: explicit key, env var, config file
(`:` and `=` separators, quoted values), explicit-key-beats-env precedence,
missing-credentials error, and `get_token()`/`is_authenticated()` behavior
pre/post authentication. Useful as executable documentation of the exact
precedence and parsing rules if this file ever needs updating.
