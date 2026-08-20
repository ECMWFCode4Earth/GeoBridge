"""
geobridge.semantic.catalog
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Dataset/variable retrieval over the full ARCO catalogue, independent of the
hand-curated theme/use_case taxonomy in ``vocabulary.yaml``.

Where ``engine.py`` matches queries against ~40 curated use cases,
``query_catalog`` matches against every (dataset, subset, variable) triple
in ``arco_snapshot.yaml`` — over a thousand short natural-language
documents — using TF-IDF + cosine similarity (scikit-learn). This means a
query can resolve to a concrete dataset/variable even when no curator has
written a use case for that exact phrasing.

Requires ``scikit-learn``, a core geobridge dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer

_OVERRIDES_PATH = Path(__file__).parent / "arco_overrides.yaml"


@dataclass(frozen=True)
class CorpusEntry:
    """One retrievable (dataset, variable) resource and the text describing it."""

    dataset_id: str
    variable: str
    text: str


# ---------------------------------------------------------------------------
# Variable alias loading
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_variable_aliases() -> dict[str, list[str]]:
    """
    Invert arco_overrides.yaml's short->long variable alias map to
    long-form names per short variable, e.g. ``"t2m" -> ["2m_temperature"]``.

    Lets a query phrased with the CDS long-form name (e.g. "2m temperature")
    still match the ARCO short-form corpus entry (e.g. "t2m").
    """
    try:
        import yaml
    except ImportError:
        return {}

    if not _OVERRIDES_PATH.exists():
        return {}
    with _OVERRIDES_PATH.open(encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}

    aliases: dict[str, list[str]] = {}
    overrides = data.get("overrides", {})
    for long_name, short_name in overrides.get("variable_aliases", {}).items():
        aliases.setdefault(short_name, []).append(long_name.replace("_", " "))
    for key, value in overrides.items():
        if key == "variable_aliases" or not isinstance(value, dict):
            continue
        for long_name, short_name in value.get("variable_aliases", {}).items():
            aliases.setdefault(short_name, []).append(long_name.replace("_", " "))
    return aliases


# ---------------------------------------------------------------------------
# Corpus construction
# ---------------------------------------------------------------------------

def _build_corpus_entries() -> list[CorpusEntry]:
    """Build one CorpusEntry per (dataset, subset, variable) triple."""
    from geobridge.modules.discover import _load_arco_snapshot

    aliases = _load_variable_aliases()
    datasets = _load_arco_snapshot()

    entries: list[CorpusEntry] = []
    for dataset_id, dataset in datasets.items():
        dataset_title = dataset.get("title", "")
        dataset_description = dataset.get("description", "")
        for subset in dataset.get("subsets", {}).values():
            subset_title = subset.get("title", "")
            for var_name, var_data in subset.get("variables", {}).items():
                parts = [
                    dataset_title,
                    dataset_description,
                    subset_title,
                    var_data.get("name", ""),
                    var_data.get("standard_name", "") or "",
                    var_name.replace("_", " "),
                    *aliases.get(var_name, []),
                ]
                text = " ".join(p for p in parts if p and p != "unknown")
                entries.append(CorpusEntry(dataset_id=dataset_id, variable=var_name, text=text))
    return entries


@lru_cache(maxsize=1)
def _load_index():
    """Build and cache the TF-IDF corpus. Returns (entries, vectorizer, matrix)."""
    entries = _build_corpus_entries()
    if not entries:
        return entries, None, None

    vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1)
    matrix = vectorizer.fit_transform([e.text for e in entries])
    return entries, vectorizer, matrix


# ---------------------------------------------------------------------------
# Public retrieval
# ---------------------------------------------------------------------------

def query_catalog(query: str, top_k: int = 15) -> list[tuple[str, str, float]]:
    """
    Rank (dataset_id, variable) pairs by TF-IDF cosine similarity to *query*.

    Returns up to *top_k* unique pairs sorted by descending score.
    """
    entries, vectorizer, matrix = _load_index()
    if not entries:
        return []

    query_vector = vectorizer.transform([query])
    # TF-IDF rows are L2-normalized, so the dot product equals cosine similarity.
    scores = (matrix @ query_vector.T).toarray().ravel()

    best: dict[tuple[str, str], float] = {}
    for entry, score in zip(entries, scores, strict=True):
        if score <= 0:
            continue
        key = (entry.dataset_id, entry.variable)
        if score > best.get(key, 0.0):
            best[key] = float(score)

    ranked = sorted(best.items(), key=lambda item: item[1], reverse=True)
    return [(dataset_id, variable, score) for (dataset_id, variable), score in ranked[:top_k]]
