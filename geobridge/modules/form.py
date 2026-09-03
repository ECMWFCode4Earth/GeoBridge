"""
geobridge.modules.form
~~~~~~~~~~~~~~~~~~~~~~

Fetches and parses the CDS form schema for any dataset in the catalogue.

The CDS STAC catalogue exposes a ``form`` link relation on every dataset
collection.  The URL points at a JSON file that describes the download
form — every parameter (variable, product_type, year, month, day, time,
pressure_level, format, etc.) with its display label, allowed values,
and UI widget type.

This module provides:

* :func:`fetch_form`          — download and parse the form for a dataset
* :func:`list_variables`      — extract the variable list with display names
* :func:`valid_years`         — extract the available years
* :func:`valid_combinations`  — fetch and parse the constraints JSON
* :class:`FormSchema`         — typed representation of a parsed form

The form is fetched once per dataset per session and cached in memory.
No form data is bundled with the library — it is always fetched live from
the ECMWF object store, which serves it with a 1-hour cache header.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FormWidget:
    """One parameter widget in the CDS download form."""

    name: str                    # parameter name, e.g. "variable"
    label: str                   # display label, e.g. "Variable"
    widget_type: str             # e.g. "StringListWidget", "DateRangeWidget"
    values: list[dict]           # list of {value, label} dicts
    required: bool = True
    details: dict = field(default_factory=dict)

    @property
    def value_list(self) -> list[str]:
        """Return raw values as a flat list."""
        return [v.get("value", v) for v in self.values
                if isinstance(v, dict) and "value" in v]

    @property
    def label_map(self) -> dict[str, str]:
        """Return {value: display_label} mapping."""
        return {v.get("value", ""): v.get("label", v.get("value", ""))
                for v in self.values if isinstance(v, dict)}


@dataclass
class FormSchema:
    """Parsed CDS form schema for one dataset."""

    dataset_id: str
    widgets: list[FormWidget] = field(default_factory=list)
    raw: list[dict] = field(default_factory=list)

    def get_widget(self, name: str) -> Optional[FormWidget]:
        """Return a widget by parameter name, or None."""
        for w in self.widgets:
            if w.name == name:
                return w
        return None

    @property
    def variable_widget(self) -> Optional[FormWidget]:
        return self.get_widget("variable")

    @property
    def product_type_widget(self) -> Optional[FormWidget]:
        return self.get_widget("product_type")

    @property
    def year_widget(self) -> Optional[FormWidget]:
        return self.get_widget("year")

    @property
    def pressure_level_widget(self) -> Optional[FormWidget]:
        return self.get_widget("pressure_level")

    def parameter_names(self) -> list[str]:
        return [w.name for w in self.widgets]

    def variables(self) -> list[dict]:
        """Return [{value, label}] for all variables."""
        w = self.variable_widget
        return w.values if w else []

    def years(self) -> list[str]:
        """Return available years as strings."""
        w = self.year_widget
        return w.value_list if w else []

    def product_types(self) -> list[dict]:
        """Return [{value, label}] for all product types."""
        w = self.product_type_widget
        return w.values if w else []

    def pressure_levels(self) -> list[str]:
        """Return available pressure levels as strings."""
        w = self.pressure_level_widget
        return w.value_list if w else []


# ---------------------------------------------------------------------------
# Fetching helpers
# ---------------------------------------------------------------------------

_FORM_CACHE: dict[str, FormSchema] = {}
_CONSTRAINTS_CACHE: dict[str, list[dict]] = {}


def _fetch_json(url: str, timeout: int = 20) -> Any:
    """HTTP GET and return parsed JSON."""
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json",
                 "User-Agent": "geobridge/0.1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


_CATALOGUE_API = "https://cds.climate.copernicus.eu/api/catalogue/v1/collections"


@lru_cache(maxsize=64)
def _fetch_live_links(cds_id: str) -> dict[str, str]:
    """Fetch the current ``form``/``constraints`` link URLs for *cds_id* live from CDS.

    The CDS catalogue rotates these to a new content-hashed filename
    whenever a dataset's form or valid-combinations rules change. The old
    hashed URL keeps serving (now stale) content instead of 404ing, so a
    URL bundled in the snapshot can go silently out of date. Resolving
    live is the only way to be sure it's current — this is cached per
    dataset per process (via :func:`clear_form_cache`) so it costs one
    extra request per dataset per session, not one per lookup.

    Returns ``{}`` on any failure (offline, CDS unreachable, unknown id).
    """
    try:
        data = _fetch_json(f"{_CATALOGUE_API}/{cds_id}", timeout=15)
    except Exception as exc:
        logger.debug("Live catalogue lookup failed for %s: %s", cds_id, exc)
        return {}

    links: dict[str, str] = {}
    for link in data.get("links", []):
        rel = link.get("rel")
        href = link.get("href", "")
        if not href:
            continue
        if rel == "form" or "form.json" in href:
            links.setdefault("form", href)
        elif rel == "constraints" or "constraints.json" in href:
            links.setdefault("constraints", href)
    return links


def _snapshot_link(dataset_id: str, cds_id: str, rel: str) -> Optional[str]:
    """Look up link relation *rel* for *dataset_id* in the bundled CDS snapshot."""
    try:
        from geobridge.modules.discover import _load_cds_snapshot
        snapshot = _load_cds_snapshot()
        entry = snapshot.get(cds_id) or snapshot.get(dataset_id)
        if entry:
            return (entry.get("links") or {}).get(rel)
    except Exception:
        pass
    return None


def _get_form_url(dataset_id: str) -> Optional[str]:
    """Look up the form URL, live from CDS first, falling back to the bundled snapshot."""
    cds_id = dataset_id.replace("_", "-")

    # 1. Live CDS catalogue API — authoritative, always current.
    url = _fetch_live_links(cds_id).get("form")
    if url:
        return url

    # 2. Bundled snapshot fallback (offline / CDS unreachable / rate-limited).
    url = _snapshot_link(dataset_id, cds_id, "form")
    if url:
        logger.debug("Using bundled snapshot form URL for %s (live lookup failed).", cds_id)
        return url

    return None


def _get_constraints_url(dataset_id: str) -> Optional[str]:
    """Look up the constraints URL, live from CDS first, falling back to the bundled snapshot."""
    cds_id = dataset_id.replace("_", "-")

    # 1. Live CDS catalogue API — authoritative, always current.
    url = _fetch_live_links(cds_id).get("constraints")
    if url:
        return url

    # 2. Bundled snapshot fallback (offline / CDS unreachable / rate-limited).
    url = _snapshot_link(dataset_id, cds_id, "constraints")
    if url:
        logger.debug(
            "Using bundled snapshot constraints URL for %s (live lookup failed).", cds_id
        )
        return url

    return None


def _parse_widget(raw: dict) -> Optional[FormWidget]:
    """Parse one form widget definition.

    CDS choice widgets store their allowed values in two different shapes:

    * ``StringListWidget`` / ``StringChoiceWidget`` — a flat ``details.values``
      list of strings, with display names in a separate ``details.labels`` map.
    * ``StringListArrayWidget`` — values split across accordion groups under
      ``details.groups``, each group carrying its own ``values`` list and
      ``labels`` map (there is no top-level ``details.values``).

    Both shapes are flattened here into a single ``{value, label}`` list,
    keeping first-seen order and dropping duplicates.
    """
    name = raw.get("name")
    label = raw.get("label", name or "")
    widget_type = raw.get("type", "")

    if not name:
        return None

    details = raw.get("details", {}) or {}

    values: list[dict] = []
    seen: set[str] = set()

    def _add(value: str, display: Optional[str] = None) -> None:
        if not value or value in seen:
            return
        seen.add(value)
        values.append({"value": value, "label": display or value})

    def _consume(items: Any, label_map: dict) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict):
                v = item.get("value", "")
                _add(v, item.get("label", label_map.get(v, v)))
            elif isinstance(item, str):
                _add(item, label_map.get(item, item))

    # 1. Flat layout (StringListWidget / StringChoiceWidget)
    _consume(details.get("values"), details.get("labels", {}) or {})

    # 2. Grouped layout (StringListArrayWidget accordion groups)
    for group_key in ("groups", "accordionGroups"):
        groups = details.get(group_key)
        if not isinstance(groups, list):
            continue
        for group in groups:
            if isinstance(group, dict):
                _consume(group.get("values"), group.get("labels", {}) or {})

    # FreeformInputWidget / DateRangeWidget / GeographicExtentWidget — no
    # predefined values, so ``values`` stays empty.
    return FormWidget(
        name=name,
        label=label,
        widget_type=widget_type,
        values=values,
        required=raw.get("required", True),
        details=details,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_form(dataset_id: str, timeout: int = 20) -> Optional[FormSchema]:
    """Fetch and parse the CDS form schema for *dataset_id*.

    Returns a :class:`FormSchema` on success, or None if the form URL
    is not in the snapshot or the fetch fails.

    Results are cached per-session in memory.

    Parameters
    ----------
    dataset_id : str
        CDS dataset identifier.  Both hyphenated and underscored forms
        are accepted.
    timeout : int
        HTTP request timeout in seconds.

    Examples
    --------
    >>> from geobridge.modules.form import fetch_form
    >>> schema = fetch_form("reanalysis-era5-single-levels")
    >>> print(schema.variables()[:3])
    [{'value': '10m_u_component_of_wind', 'label': '10m u-component of wind'}, ...]
    """
    # Normalise to hyphenated (CDS) form for cache key
    cds_id = dataset_id.replace("_", "-")

    if cds_id in _FORM_CACHE:
        return _FORM_CACHE[cds_id]

    url = _get_form_url(dataset_id)
    if not url:
        logger.warning(
            "No form URL found for %s. "
            "Run scripts/refresh_catalogue.py to populate the CDS snapshot.",
            dataset_id,
        )
        return None

    try:
        raw = _fetch_json(url, timeout=timeout)
    except urllib.error.URLError as exc:
        logger.warning("Could not fetch form for %s: %s", dataset_id, exc)
        return None
    except Exception as exc:
        logger.warning("Error parsing form for %s: %s", dataset_id, exc)
        return None

    widgets: list[FormWidget] = []
    if isinstance(raw, list):
        for item in raw:
            w = _parse_widget(item)
            if w:
                widgets.append(w)

    schema = FormSchema(dataset_id=cds_id, widgets=widgets, raw=raw)
    _FORM_CACHE[cds_id] = schema
    logger.info(
        "Fetched form for %s: %d widgets, %d variables",
        cds_id, len(widgets),
        len(schema.variables()),
    )
    return schema


def fetch_constraints(dataset_id: str, timeout: int = 20) -> list[dict]:
    """Fetch the constraints JSON for *dataset_id*.

    Returns a list of valid parameter combination objects.  Each object
    is a dict of parameter-name → list-of-valid-values.  The combination
    is valid only if ALL parameters in the object are satisfied jointly.

    Returns an empty list if the constraints URL is missing or the fetch
    fails.

    Examples
    --------
    >>> from geobridge.modules.form import fetch_constraints
    >>> combos = fetch_constraints("reanalysis-era5-single-levels")
    >>> # Find all product types
    >>> pts = set()
    >>> for c in combos:
    ...     pts.update(c.get("product_type", []))
    >>> print(sorted(pts))
    ['ensemble_mean', 'ensemble_members', 'ensemble_spread',
     'monthly_averaged_reanalysis', 'reanalysis']
    """
    cds_id = dataset_id.replace("_", "-")

    if cds_id in _CONSTRAINTS_CACHE:
        return _CONSTRAINTS_CACHE[cds_id]

    url = _get_constraints_url(dataset_id)
    if not url:
        logger.warning("No constraints URL for %s.", dataset_id)
        return []

    try:
        data = _fetch_json(url, timeout=timeout)
        result = data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("Could not fetch constraints for %s: %s", dataset_id, exc)
        return []

    _CONSTRAINTS_CACHE[cds_id] = result
    return result


def valid_variables_for_product_type(
    dataset_id: str,
    product_type: str,
) -> list[str]:
    """Return variables valid for *product_type* from the constraints.

    Useful for filtering the variable list when the user has selected a
    specific product type (e.g. show only variables available in the
    monthly averaged reanalysis, not all ERA5 variables).

    Returns an empty list if constraints are unavailable.
    """
    combos = fetch_constraints(dataset_id)
    result: set[str] = set()
    for combo in combos:
        pts = combo.get("product_type", [])
        if product_type in pts:
            result.update(combo.get("variable", []))
    return sorted(result)


def validate_request(dataset_id: str, request: dict) -> list[str]:
    """Validate a CDS request dict against the form schema and constraints.

    Checks two things:

    1. **Allowed values** — every value in the request must appear in the
       form's enum for that parameter (uses :func:`fetch_form`).
    2. **Valid combinations** — the combination of values must match at
       least one entry in the constraints JSON (uses :func:`fetch_constraints`).
       Skipped if no constraints URL is available for the dataset.

    Parameters
    ----------
    dataset_id : str
        CDS dataset identifier.
    request : dict
        CDS API request parameters (same dict passed to ``cds_to_geotiff``).

    Returns
    -------
    list[str]
        List of human-readable error strings.  Empty list means the request
        looks valid.

    Examples
    --------
    >>> from geobridge.modules.form import validate_request
    >>> errors = validate_request("derived-utci-historical", {
    ...     "product_type": ["consolidated_dataset"],
    ...     "variable": ["universal_thermal_climate_index"],
    ...     "year": ["2025"], "month": ["01"], "day": ["01"],
    ...     "data_format": "grib",
    ... })
    >>> if errors:
    ...     for e in errors: print(e)
    """
    errors: list[str] = []

    # --- 1. Allowed-values check via form schema ---
    schema = fetch_form(dataset_id)
    if schema:
        for param, val in request.items():
            if param == "data_format":
                continue
            widget = schema.get_widget(param)
            if widget is None or not widget.value_list:
                continue  # unknown / free-form parameter — skip
            allowed = set(widget.value_list)
            submitted = [val] if isinstance(val, str) else list(val)
            bad = [v for v in submitted if v not in allowed]
            if bad:
                errors.append(
                    f"'{param}': invalid value(s) {bad}. "
                    f"Allowed: {sorted(allowed)}"
                )
    else:
        logger.warning(
            "Form schema not available for %s — skipping allowed-values check.",
            dataset_id,
        )

    # --- 2. Combination check via constraints JSON ---
    constraints = fetch_constraints(dataset_id)
    if constraints:
        # Normalise request values to sets of strings for comparison
        req_sets: dict[str, set[str]] = {}
        for param, val in request.items():
            if param == "data_format":
                continue
            req_sets[param] = (
                {val} if isinstance(val, str) else set(val)
            )

        def _combo_matches(combo: dict) -> bool:
            for param, allowed_vals in combo.items():
                if param not in req_sets:
                    continue
                if not req_sets[param].intersection(allowed_vals):
                    return False
            return True

        if not any(_combo_matches(c) for c in constraints):
            errors.append(
                "The parameter combination is not valid for this dataset. "
                "Check the CDS portal for allowed combinations: "
                f"https://cds.climate.copernicus.eu/datasets/"
                f"{dataset_id.replace('_', '-')}"
            )

    return errors


def clear_form_cache():
    """Clear the in-memory form, constraints, and live-link caches."""
    _FORM_CACHE.clear()
    _CONSTRAINTS_CACHE.clear()
    _fetch_live_links.cache_clear()
