"""Guard: every dataset/variable named in vocabulary.yaml must exist in the
ARCO snapshot (after applying the arco_overrides.yaml alias map).

This turns "someone edited the vocabulary without checking it against the
real Zarr stores" into a CI failure. See vocabulary.yaml's header note:
"Verified against arco_snapshot.yaml".
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_SEMANTIC = Path(__file__).resolve().parents[2] / "geobridge" / "semantic"


def _load(name: str) -> dict:
    with (_SEMANTIC / name).open(encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}


VOCAB = _load("vocabulary.yaml")
SNAPSHOT = _load("arco_snapshot.yaml").get("datasets", {})
OVERRIDES = _load("arco_overrides.yaml").get("overrides", {})


def _alias_map(dataset_id: str) -> dict[str, str]:
    """Long/CDS name -> ARCO short name, global plus per-dataset overrides."""
    aliases = dict(OVERRIDES.get("variable_aliases", {}))
    ds = OVERRIDES.get(dataset_id)
    if isinstance(ds, dict):
        aliases.update(ds.get("variable_aliases", {}))
    return aliases


def _subsets_of(dataset_id: str) -> dict:
    return (SNAPSHOT.get(dataset_id) or {}).get("subsets") or {}


def _variables_in(dataset_id: str) -> set[str]:
    names: set[str] = set()
    for subset in _subsets_of(dataset_id).values():
        names |= set((subset.get("variables") or {}).keys())
    return names


def _dataset_variable_pairs():
    """Yield (use_case_id, field, dataset_id, subset, variable) for every reference.

    ``subset`` is None for the fusion dataset (vocabulary carries no
    ``fusion_subset`` field), so those are only checked at dataset level.
    """
    for uc_id, uc in (VOCAB.get("use_cases") or {}).items():
        yield uc_id, "dataset", uc.get("dataset"), uc.get("subset"), uc.get("variable")
        if uc.get("fusion_dataset"):
            yield (
                uc_id,
                "fusion_dataset",
                uc.get("fusion_dataset"),
                None,
                uc.get("fusion_variable"),
            )


PAIRS = [p for p in _dataset_variable_pairs() if p[2]]


@pytest.mark.parametrize(
    "uc_id,field,dataset_id,subset,variable",
    PAIRS,
    ids=[f"{uc}:{f}" for uc, f, _, _, _ in PAIRS],
)
def test_vocabulary_pair_exists_in_arco(uc_id, field, dataset_id, subset, variable):
    assert dataset_id in SNAPSHOT, (
        f"use_case '{uc_id}' references dataset '{dataset_id}' "
        f"which is not in arco_snapshot.yaml"
    )
    assert variable, f"use_case '{uc_id}' has no variable for {field}"

    resolved = _alias_map(dataset_id).get(variable, variable)

    if subset is not None:
        subsets = _subsets_of(dataset_id)
        assert subset in subsets, (
            f"use_case '{uc_id}': subset '{subset}' is not a subset of "
            f"'{dataset_id}'. Available: {sorted(subsets)}"
        )
        subset_vars = set((subsets[subset].get("variables") or {}).keys())
        assert resolved in subset_vars, (
            f"use_case '{uc_id}': variable '{variable}' (resolved to '{resolved}') "
            f"is not served by subset '{subset}' of '{dataset_id}'. "
            f"That subset has: {sorted(subset_vars)}"
        )
    else:
        available = _variables_in(dataset_id)
        assert resolved in available, (
            f"use_case '{uc_id}': variable '{variable}' (resolved to '{resolved}') "
            f"is not served by any subset of '{dataset_id}'. "
            f"Available: {sorted(available)}"
        )


@pytest.mark.parametrize("theme_id", list((VOCAB.get("themes") or {}).keys()))
def test_theme_use_case_references_resolve(theme_id):
    theme = VOCAB["themes"][theme_id]
    known = set((VOCAB.get("use_cases") or {}).keys())
    for uc_id in theme.get("use_cases", []):
        assert uc_id in known, (
            f"theme '{theme_id}' lists use_case '{uc_id}' "
            f"which has no definition under use_cases:"
        )


@pytest.mark.parametrize("uc_id", list((VOCAB.get("use_cases") or {}).keys()))
def test_use_case_theme_back_reference(uc_id):
    uc = VOCAB["use_cases"][uc_id]
    theme_id = uc.get("theme")
    themes = VOCAB.get("themes") or {}
    assert theme_id in themes, (
        f"use_case '{uc_id}' has theme '{theme_id}' with no matching themes: entry"
    )
    assert uc_id in themes[theme_id].get("use_cases", []), (
        f"use_case '{uc_id}' is not listed under theme '{theme_id}'.use_cases"
    )
