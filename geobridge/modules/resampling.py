"""
geobridge.modules.resampling
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Semantics-aware choice of resampling kernel for ``fuse()``.

No single kernel is right for every variable or every direction of grid
change. Bilinear interpolates between source values, so on a categorical
layer it invents class values that never existed in the source (a land-sea
mask comes back with fractional "classes"). Mean-type kernels (bilinear,
average) also dilute peaks, which is wrong for maxima such as wind gusts.

For continuous data on a large downsample, ``average`` is chosen explicitly
rather than left to bilinear. Under GDAL 3.10 (the version this was tested
against) the warper already widens the bilinear kernel with the downsampling
ratio and behaves like a tent-weighted aggregate; a true fixed 2x2 stencil
(as in scipy or ``xarray.interp``) would read only four source pixels per
target pixel. ``average`` makes the result independent of that backend detail.

The kernel here follows the variable's physical meaning and the direction of
the grid change:

============  ==========================  ==========
semantics     downsampling                upsampling
============  ==========================  ==========
continuous    average                     bilinear
categorical   mode                        nearest
extrema       max                         nearest
minima        min                         nearest
============  ==========================  ==========

Semantics are inferred from the variable name, covering both CDS long names
(``2m_temperature``) and GRIB short codes (``t2m``).

Limits
------
* ``average`` preserves the areal *mean*, not summed totals. Precipitation
  and fluxes need conservative regridding (e.g. xESMF) if totals must be
  preserved exactly.
* Inference is a name heuristic that defaults to ``continuous``. A categorical
  layer with an unrecognised name needs ``method="nearest"`` (or ``"mode"``)
  passed explicitly.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

CONTINUOUS = "continuous"
CATEGORICAL = "categorical"
EXTREMA = "extrema"
MINIMA = "minima"

# Kernels a caller may request explicitly (rasterio.enums.Resampling members).
KERNELS = ("nearest", "bilinear", "cubic", "average", "mode", "max", "min", "med")

_DOWNSAMPLE = {CONTINUOUS: "average", CATEGORICAL: "mode", EXTREMA: "max", MINIMA: "min"}
_UPSAMPLE = {CONTINUOUS: "bilinear", CATEGORICAL: "nearest", EXTREMA: "nearest", MINIMA: "nearest"}

# Target/source spacing ratios within this fraction of 1 count as "same grid".
_SAME_TOL = 0.05


# ---------------------------------------------------------------------------
# Semantics inference
# ---------------------------------------------------------------------------

# Whole-token matches after punctuation is collapsed, so "Land-sea mask",
# "land_sea_mask" and "land sea mask" are equivalent. Bare "max"/"min" are
# deliberately absent: they appear in aggregation suffixes ("2m_temperature_
# daily_max") that describe a temporal statistic of a continuous field.
_CATEGORICAL_TOKENS = frozenset({
    "mask", "class", "classes", "classification", "category", "categories",
    "type", "flag", "biome", "landcover", "landuse", "lccs",
    # GRIB short names: land-sea mask, high/low vegetation type, soil type,
    # precipitation type
    "lsm", "tvh", "tvl", "slt", "ptype",
})
_CATEGORICAL_PHRASES = ("land_cover", "land_use")

_MAXIMA_TOKENS = frozenset({"maximum", "maxima", "gust", "gusts", "peak"})
_MINIMA_TOKENS = frozenset({"minimum", "minima"})
# GRIB short names, with optional 3/6/24 h suffix: mx2t, mx2t24, mxtpr, i10fg, fg10
_MAXIMA_CODE = re.compile(r"^(?:mx2t|mxtpr|i?10fg|fg10)\d*$")
_MINIMA_CODE = re.compile(r"^(?:mn2t|mntpr)\d*$")


def _classify(name: str) -> Optional[str]:
    """Return the semantics a single name signals, or None if it says nothing."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]
    joined = "_" + "_".join(tokens) + "_"

    if _CATEGORICAL_TOKENS.intersection(tokens) or any(
        f"_{phrase}_" in joined for phrase in _CATEGORICAL_PHRASES
    ):
        return CATEGORICAL
    if _MINIMA_TOKENS.intersection(tokens) or any(_MINIMA_CODE.match(t) for t in tokens):
        return MINIMA
    if _MAXIMA_TOKENS.intersection(tokens) or any(_MAXIMA_CODE.match(t) for t in tokens):
        return EXTREMA
    return None


def infer_semantics(*names: Optional[str]) -> str:
    """
    Infer what a variable's values mean from one or more identifying strings.

    Returns ``"continuous"``, ``"categorical"``, ``"extrema"`` or ``"minima"``.
    The first name that signals something specific wins; if none does, the
    result is ``"continuous"``.
    """
    for name in names:
        if name:
            kind = _classify(str(name))
            if kind is not None:
                return kind
    return CONTINUOUS


# ---------------------------------------------------------------------------
# Kernel selection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResamplingChoice:
    """The kernel picked for one layer, and the reasoning behind it."""

    method: str      # rasterio Resampling member name
    semantics: str   # continuous | categorical | extrema | minima
    direction: str   # "down" | "up" | "same"
    ratio: float     # target_res / source_res; > 1 means downsampling
    source: str      # "auto" or "user"


def select_method(semantics: str, direction: str) -> str:
    """Return the kernel for a semantics class and grid-change direction."""
    table = _DOWNSAMPLE if direction == "down" else _UPSAMPLE
    try:
        return table[semantics]
    except KeyError:
        raise ValueError(
            f"Unknown semantics {semantics!r}; expected one of {sorted(_DOWNSAMPLE)}"
        ) from None


def resolve_method(
    names,
    source_res: float,
    target_res: float,
    method: str = "auto",
    label: Optional[str] = None,
) -> ResamplingChoice:
    """
    Choose the resampling kernel for one layer.

    Parameters
    ----------
    names : iterable of str
        Strings that may identify the variable, most specific first.
    source_res, target_res : float
        Grid spacing of the layer and of the target grid, in the same unit.
    method : 'auto' or one of ``KERNELS``
        ``'auto'`` picks from semantics and direction; anything else is used
        as given.
    label : str, optional
        Layer name used in log messages.
    """
    if method != "auto" and method not in KERNELS:
        raise ValueError(f"method must be 'auto' or one of {KERNELS}; got {method!r}")

    ratio = target_res / source_res if source_res else 1.0
    if ratio > 1 + _SAME_TOL:
        direction = "down"
    elif ratio < 1 - _SAME_TOL:
        direction = "up"
    else:
        direction = "same"

    semantics = infer_semantics(*names)
    if method == "auto":
        chosen, origin = select_method(semantics, direction), "auto"
    else:
        chosen, origin = method, "user"

    if direction == "up":
        logger.warning(
            "%s: upsampling %.4g° → %.4g° (%.1f× finer). Interpolated values "
            "add no information beyond the source grid; apparent sub-grid "
            "structure is an artefact.",
            label or "layer", source_res, target_res, 1.0 / ratio,
        )

    return ResamplingChoice(
        method=chosen, semantics=semantics, direction=direction,
        ratio=float(ratio), source=origin,
    )


def to_rasterio(method: str):
    """Map a kernel name to the corresponding ``rasterio.enums.Resampling``."""
    from rasterio.enums import Resampling
    return Resampling[method]


# ---------------------------------------------------------------------------
# QA
# ---------------------------------------------------------------------------

def check_resampling(
    source,
    result,
    semantics: str = CONTINUOUS,
    mean_tol_pct: float = 1.0,
) -> dict:
    """
    Compare a layer before and after resampling and flag what went wrong.

    ``source`` should cover the same footprint as ``result`` (clip it first if
    the target grid is a sub-region), otherwise the mean shift reflects the
    change of area rather than the kernel.

    Parameters
    ----------
    source, result : array-like or xarray.DataArray
        Values before and after resampling. Non-finite values are ignored.
    semantics : str
        As returned by :func:`infer_semantics`. Decides which checks apply.
    mean_tol_pct : float
        Continuous layers are flagged when the mean moves by more than this
        percentage of ``max(|source mean|, source std)``. The std term keeps
        the check meaningful for fields whose mean is near zero.

    Returns
    -------
    dict
        Summary statistics plus ``flags``, a list that may contain:

        ``"mean_shift"``
            continuous layer whose mean moved by more than ``mean_tol_pct``.
        ``"invented_classes"``
            categorical layer whose result holds values absent from the source.
        ``"no_valid_data"``
            either side has no finite values.

        A mean shift is expected for extrema/minima and meaningless for
        categorical data, so it is only flagged for continuous layers.
    """
    import numpy as np

    def _finite(x):
        arr = np.asarray(getattr(x, "values", x), dtype=float).ravel()
        return arr[np.isfinite(arr)]

    src, res = _finite(source), _finite(result)
    stats: dict = {
        "semantics": semantics,
        "n_source": int(src.size),
        "n_result": int(res.size),
        "flags": [],
    }
    if src.size == 0 or res.size == 0:
        stats["flags"].append("no_valid_data")
        return stats

    mean_src, mean_res = float(src.mean()), float(res.mean())
    std_src, std_res = float(src.std()), float(res.std())
    scale = max(abs(mean_src), std_src)
    shift_pct = 100.0 * (mean_res - mean_src) / scale if scale > 0 else 0.0
    stats.update(
        mean_source=mean_src, mean_result=mean_res,
        std_source=std_src, std_result=std_res,
        mean_shift_pct=shift_pct,
    )

    if semantics == CATEGORICAL:
        invented = np.setdiff1d(np.unique(res), np.unique(src))
        stats["n_invented_classes"] = int(invented.size)
        stats["invented_examples"] = invented[:10].tolist()
        if invented.size:
            stats["flags"].append("invented_classes")
    elif semantics == CONTINUOUS and abs(shift_pct) > mean_tol_pct:
        stats["flags"].append("mean_shift")

    return stats
