"""
examples/example_form.py
~~~~~~~~~~~~~~~~~~~~~~~~

Demonstrates every public function in geobridge.modules.form using the
ERA5 single-levels dataset (one of the most commonly used CDS datasets).

Run from the repo root:
    python examples/example_form.py

What it checks:
  1. fetch_form()              — downloads and parses the form schema
  2. FormSchema.parameter_names()  — lists all available parameters
  3. FormSchema.variables()        — full variable list with display labels
  4. FormSchema.years()            — available years
  5. FormSchema.product_types()    — available product types
  6. FormSchema.pressure_levels()  — pressure levels (if present)
  7. FormWidget.label_map          — {value: label} mapping for one widget
  8. fetch_constraints()       — downloads the valid-combination rules
  9. valid_variables_for_product_type() — filters variables by product type
 10. clear_form_cache()        — resets the in-memory cache
"""

import logging
import sys

# Show INFO logs so we can see what the library is doing under the hood
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(name)s  %(message)s",
)

from geobridge.modules.form import (
    FormSchema,
    clear_form_cache,
    fetch_constraints,
    fetch_form,
    valid_variables_for_product_type,
)

#DATASET = "reanalysis-era5-single-levels"
DATASET = "reanalysis-era5-land"


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# 1. Fetch and parse the form
# ---------------------------------------------------------------------------
section("1. fetch_form()")
schema: FormSchema | None = fetch_form(DATASET)

if schema is None:
    print("ERROR: fetch_form() returned None.")
    print("Make sure the CDS snapshot is populated:")
    print("    python scripts/refresh_catalogue.py")
    sys.exit(1)

print(f"dataset_id : {schema.dataset_id}")
print(f"widgets    : {len(schema.widgets)}")
print(f"raw items  : {len(schema.raw)}")

# ---------------------------------------------------------------------------
# 2. Parameter names
# ---------------------------------------------------------------------------
section("2. FormSchema.parameter_names()")
names = schema.parameter_names()
print(f"Parameters ({len(names)}): {names}")

# ---------------------------------------------------------------------------
# 3. Variables
# ---------------------------------------------------------------------------
section("3. FormSchema.variables()  (first 5 shown)")
variables = schema.variables()
print(f"Total variables: {len(variables)}")
for v in variables[:5]:
    print(f"  {v['value']!r:45s}  →  {v['label']}")

# ---------------------------------------------------------------------------
# 4. Years
# ---------------------------------------------------------------------------
section("4. FormSchema.years()")
years = schema.years()
print(f"Total years: {len(years)}")
if years:
    print(f"Range: {years[0]} – {years[-1]}")
    print(f"First 5: {years[:5]}")

# ---------------------------------------------------------------------------
# 5. Product types
# ---------------------------------------------------------------------------
section("5. FormSchema.product_types()")
pts = schema.product_types()
print(f"Product types ({len(pts)}):")
for pt in pts:
    print(f"  {pt['value']!r:45s}  →  {pt['label']}")

# ---------------------------------------------------------------------------
# 6. Pressure levels
# ---------------------------------------------------------------------------
section("6. FormSchema.pressure_levels()")
levels = schema.pressure_levels()
if levels:
    print(f"Pressure levels ({len(levels)}): {levels[:10]} ...")
else:
    print("No pressure_level widget on this dataset (expected for single-levels).")

# ---------------------------------------------------------------------------
# 7. Widget label_map and value_list
# ---------------------------------------------------------------------------
section("7. FormWidget.label_map  (variable widget, first 5 entries)")
var_widget = schema.variable_widget
if var_widget:
    print(f"Widget name      : {var_widget.name}")
    print(f"Widget label     : {var_widget.label}")
    print(f"Widget type      : {var_widget.widget_type}")
    print(f"Required         : {var_widget.required}")
    lmap = var_widget.label_map
    for k, v in list(lmap.items())[:5]:
        print(f"  {k!r:45s}  →  {v!r}")
    print(f"value_list[:5]   : {var_widget.value_list[:5]}")
else:
    print("No variable widget found.")

# ---------------------------------------------------------------------------
# 8. FormSchema.get_widget() — arbitrary widget lookup
# ---------------------------------------------------------------------------
section("8. FormSchema.get_widget('year')")
year_widget = schema.get_widget("year")
if year_widget:
    print(f"name={year_widget.name!r}, type={year_widget.widget_type!r}, "
          f"values={len(year_widget.values)}")
else:
    print("No 'year' widget found.")

# ---------------------------------------------------------------------------
# 9. fetch_constraints()
# ---------------------------------------------------------------------------
section("9. fetch_constraints()")
combos = fetch_constraints(DATASET)
print(f"Constraint combinations: {len(combos)}")
if combos:
    print(f"Keys in first combo: {list(combos[0].keys())}")
    # Collect all unique product types from constraints
    all_pts: set[str] = set()
    for c in combos:
        all_pts.update(c.get("product_type", []))
    print(f"Product types in constraints: {sorted(all_pts)}")

    # Print every combination, truncating long value lists for readability
    print(f"\n  {'#':<5}  Combination details")
    print(f"  {'-'*56}")
    for i, combo in enumerate(combos):
        print(f"\n  Combo #{i + 1}")
        for param, vals in combo.items():
            if isinstance(vals, list):
                if len(vals) <= 6:
                    display = ", ".join(str(v) for v in vals)
                else:
                    display = (
                        ", ".join(str(v) for v in vals[:3])
                        + f"  … ({len(vals)} total) … "
                        + ", ".join(str(v) for v in vals[-3:])
                    )
            else:
                display = str(vals)
            print(f"    {param:<30s}: {display}")

# ---------------------------------------------------------------------------
# 10. valid_variables_for_product_type()
# ---------------------------------------------------------------------------
section("10. valid_variables_for_product_type('reanalysis')")
reanalysis_vars = valid_variables_for_product_type(DATASET, "reanalysis")
print(f"Variables valid for 'reanalysis': {len(reanalysis_vars)}")
if reanalysis_vars:
    print(f"First 5: {reanalysis_vars[:5]}")

section("10b. valid_variables_for_product_type('monthly_averaged_reanalysis')")
monthly_vars = valid_variables_for_product_type(
    DATASET, "monthly_averaged_reanalysis"
)
print(f"Variables valid for 'monthly_averaged_reanalysis': {len(monthly_vars)}")
if monthly_vars:
    print(f"First 5: {monthly_vars[:5]}")

# ---------------------------------------------------------------------------
# 11. clear_form_cache() and re-fetch to confirm caching behaviour
# ---------------------------------------------------------------------------
section("11. clear_form_cache() + re-fetch")
clear_form_cache()
print("Cache cleared.")
schema2 = fetch_form(DATASET)
if schema2 is not None:
    print(f"Re-fetched OK — {len(schema2.widgets)} widgets.")
    assert schema2 is not schema, "Expected a new object after cache clear"
    print("New FormSchema object confirmed (not the cached one).")
else:
    print("Re-fetch returned None — check network / snapshot.")

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
print("\nAll checks complete.")
