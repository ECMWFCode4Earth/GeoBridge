#!/usr/bin/env python3
"""
download_non_arco.py
====================
Download a non-ARCO Copernicus dataset as GeoTIFF via the CDS API.

Accepts a dataset name (hyphens or underscores) and a set of request
parameters, then uses geobridge.cds_to_geotiff() to submit the job,
poll for completion, download the result, and write a GeoTIFF.

Requirements:
  - CDS API key in ~/.cdsapirc  (or CDSAPI_KEY env var)
  - pip install -e ".[zarr]"

Usage examples
--------------
# Minimal — just dataset and variable (uses sensible defaults for dates):
  python examples/download_non_arco.py \\
      --dataset reanalysis-era5-pressure-levels \\
      --variable temperature \\
      --pressure-level 500 \\
      --year 2023 --month 07 --day 15 \\
      --output era5_500hpa.tif

# With spatial subset:
  python examples/download_non_arco.py \\
      --dataset satellite-sea-surface-temperature \\
      --variable sea_surface_temperature \\
      --year 2023 --month 06 \\
      --bbox 20 35 28 42 \\
      --output sst_med.tif

# Pass arbitrary extra CDS parameters as key=value pairs:
  python examples/download_non_arco.py \\
      --dataset reanalysis-era5-pressure-levels \\
      --variable temperature \\
      --pressure-level 500 850 \\
      --year 2023 --month 07 --day 15 \\
      --time 12:00 \\
      --extra product_type=reanalysis \\
      --output era5_pl.tif
"""

import argparse
import json
import sys
from pathlib import Path

# Allow running from repo root without installing
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _progress(msg: str) -> None:
    print(f"  ↳ {msg}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download a non-ARCO Copernicus dataset as GeoTIFF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Dataset / variable
    parser.add_argument("--dataset", required=True,
                        help="CDS dataset ID (hyphens or underscores)")
    parser.add_argument("--variable", nargs="+",
                        help="Variable name(s) to request. Omit to list available variables.")
    parser.add_argument("--pressure-level", nargs="+", dest="pressure_level",
                        help="Pressure level(s) in hPa, e.g. 500 850")

    # Time selection
    parser.add_argument("--year",  nargs="+", help="Year(s), e.g. 2023")
    parser.add_argument("--month", nargs="+", help="Month(s) as zero-padded strings, e.g. 07")
    parser.add_argument("--day",   nargs="+", help="Day(s) as zero-padded strings, e.g. 01 15")
    parser.add_argument("--time",  nargs="+", default=["12:00"],
                        help="Hour(s) as HH:MM strings (default: 12:00)")

    # Spatial subset
    parser.add_argument("--bbox", nargs=4, type=float,
                        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                        help="Spatial bounding box in WGS-84 (applied post-download)")

    # Output
    parser.add_argument("--output", default="output.tif",
                        help="Output GeoTIFF path (default: output.tif)")
    parser.add_argument("--no-cog", action="store_true",
                        help="Write a plain GeoTIFF instead of Cloud Optimized GeoTIFF")

    # Extra CDS request parameters
    parser.add_argument("--extra", nargs="*", metavar="KEY=VALUE",
                        help="Additional CDS request parameters as key=value pairs")

    # Discovery info
    parser.add_argument("--info", action="store_true",
                        help="Print dataset info and exit without downloading")
    parser.add_argument("--timeout", type=float, default=3600,
                        help="Max seconds to wait for the CDS job (default: 3600)")

    args = parser.parse_args()

    import geobridge as gb

    # ── Authenticate ────────────────────────────────────────────────────────
    try:
        gb.authenticate()
        print(f"  Authenticated (key: …{gb.get_token()[-6:]})")
    except gb.AuthenticationError as exc:
        sys.exit(f"Authentication failed: {exc}\nSet up ~/.cdsapirc with your CDS API key.")

    # ── Discover dataset ────────────────────────────────────────────────────
    ds = gb.discover_one(args.dataset)
    if ds is None:
        sys.exit(
            f"Dataset '{args.dataset}' not found in the GeoBridge catalogue.\n"
            "Run:  python -c \"import geobridge as gb; [print(d.id) for d in gb.discover()]\"\n"
            "to list all known datasets."
        )

    print(f"\n  Dataset : {ds.id}")
    print(f"  Title   : {ds.title}")
    print(f"  Service : {ds.service}")
    print(f"  Zarr    : {'yes' if ds.has_zarr else 'no'}")
    print(f"  CDS API : {'yes' if ds.has_cds_retrieve else 'no'}")

    if ds.has_zarr:
        print(
            f"\n  NOTE: '{ds.id}' IS available as ARCO Zarr.\n"
            "  For faster extraction use:  gb.zarr_to_geotiff()\n"
            "  Proceeding with CDS API path as requested.\n"
        )

    if not ds.has_cds_retrieve and not ds.cds_form_url:
        sys.exit(
            f"\n  Dataset '{ds.id}' has no CDS retrieve endpoint in the catalogue.\n"
            "  It may not be downloadable via the CDS API."
        )

    # ── Fetch form schema — always try when variable is omitted or --info ────
    show_variables = args.info or not args.variable
    schema = None
    if ds.cds_form_url:
        try:
            cds_id = ds.id.replace("_", "-")
            schema = gb.fetch_form(cds_id)
        except Exception as exc:
            if show_variables:
                print(f"  (Could not fetch form schema: {exc})")

    if show_variables:
        if schema:
            variables = schema.variables()
            print(f"\n  Available variables ({len(variables)}):")
            for v in variables:
                print(f"    {v.get('value',''):40s}  {v.get('label','')}")
            years = schema.years()
            if years:
                print(f"\n  Year range : {years[0]} – {years[-1]}")
            params = schema.parameter_names()
            if params:
                print(f"  Parameters : {', '.join(params)}")
        else:
            print("  No form schema available for this dataset.")
        print()
        if not args.variable:
            print("  Pass --variable <name> to start a download.\n")
            return
        # --info was set but variable was also provided — continue to download

    if not args.variable:
        return

    # ── Build CDS request ───────────────────────────────────────────────────
    request: dict = {
        "variable": args.variable,
        "data_format": "netcdf",
    }

    if args.year:
        request["year"] = args.year
    if args.month:
        request["month"] = args.month
    if args.day:
        request["day"] = args.day
    if args.time:
        request["time"] = args.time
    if args.pressure_level:
        request["pressure_level"] = args.pressure_level

    # Extra key=value pairs
    if args.extra:
        for kv in args.extra:
            if "=" not in kv:
                sys.exit(f"--extra argument must be KEY=VALUE, got: {kv!r}")
            k, _, v = kv.partition("=")
            # Support comma-separated lists: key=a,b,c → ["a", "b", "c"]
            parts = [p.strip() for p in v.split(",")]
            request[k.strip()] = parts if len(parts) > 1 else parts[0]

    bbox = tuple(args.bbox) if args.bbox else None

    print(f"\n  Request:")
    print(f"    {json.dumps(request, indent=6)[1:-1].strip()}")
    if bbox:
        print(f"  Bbox    : {bbox}")
    print(f"  Output  : {args.output}\n")

    # ── Download ─────────────────────────────────────────────────────────────
    try:
        result = gb.cds_to_geotiff(
            dataset=args.dataset,
            request=request,
            variable=args.variable[0],
            bbox=bbox,
            output_path=args.output,
            timeout=args.timeout,
            progress_callback=_progress,
            cog=not args.no_cog,
        )
        size_kb = result.stat().st_size / 1024
        print(f"\n  GeoTIFF written: {result}  ({size_kb:.1f} KB)")

    except gb.CdsApiError as exc:
        sys.exit(f"\n  CDS API error:\n{exc}")
    except gb.CdsJobTimeout as exc:
        sys.exit(f"\n  Timeout:\n{exc}")
    except Exception as exc:
        sys.exit(f"\n  Unexpected error: {exc}")


if __name__ == "__main__":
    main()
