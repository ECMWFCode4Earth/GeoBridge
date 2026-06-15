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


def _get_form_url(dataset_id: str) -> Optional[str]:
    """Look up the form URL, first from the local snapshot then live from CDS."""
    cds_id = dataset_id.replace("_", "-")

    # 1. Local snapshot (fast, no network)
    try:
        from geobridge.modules.discover import _load_cds_snapshot
        snapshot = _load_cds_snapshot()
        entry = snapshot.get(cds_id) or snapshot.get(dataset_id)
        if entry:
            url = (entry.get("links") or {}).get("form")
            if url:
                return url
    except Exception:
        pass

    # 2. Live CDS catalogue API fallback (dataset not in bundled snapshot)
    try:
        collection_url = f"{_CATALOGUE_API}/{cds_id}"
        data = _fetch_json(collection_url, timeout=15)
        for link in data.get("links", []):
            if link.get("rel") == "form" or "form.json" in link.get("href", ""):
                return link["href"]
    except Exception as exc:
        logger.debug("Live form URL lookup failed for %s: %s", cds_id, exc)

    return None


def _get_constraints_url(dataset_id: str) -> Optional[str]:
    """Look up the constraints URL, first from the local snapshot then live from CDS."""
    cds_id = dataset_id.replace("_", "-")

    # 1. Local snapshot
    try:
        from geobridge.modules.discover import _load_cds_snapshot
        snapshot = _load_cds_snapshot()
        entry = snapshot.get(cds_id) or snapshot.get(dataset_id)
        if entry:
            url = (entry.get("links") or {}).get("constraints")
            if url:
                return url
    except Exception:
        pass

    # 2. Live CDS catalogue API fallback
    try:
        collection_url = f"{_CATALOGUE_API}/{cds_id}"
        data = _fetch_json(collection_url, timeout=15)
        for link in data.get("links", []):
            if link.get("rel") == "constraints" or "constraints.json" in link.get("href", ""):
                return link["href"]
    except Exception as exc:
        logger.debug("Live constraints URL lookup failed for %s: %s", cds_id, exc)

    return None


def _parse_widget(raw: dict) -> Optional[FormWidget]:
    """Parse one form widget definition."""
    name = raw.get("name")
    label = raw.get("label", name or "")
    widget_type = raw.get("type", "")

    if not name:
        return None

    # Extract values — different widget types store them differently
    values: list[dict] = []

    # StringListWidget / StringChoiceWidget
    if "details" in raw:
        details = raw["details"]
        for item in details.get("values", []):
            if isinstance(item, dict):
                values.append({
                    "value": item.get("value", ""),
                    "label": item.get("label", item.get("value", "")),
                })
            elif isinstance(item, str):
                values.append({"value": item, "label": item})

    # FreeformInputWidget / DateRangeWidget — no predefined values
    return FormWidget(
        name=name,
        label=label,
        widget_type=widget_type,
        values=values,
        required=raw.get("required", True),
        details=raw.get("details", {}),
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


def clear_form_cache():
    """Clear the in-memory form and constraints cache."""
    _FORM_CACHE.clear()
    _CONSTRAINTS_CACHE.clear()
