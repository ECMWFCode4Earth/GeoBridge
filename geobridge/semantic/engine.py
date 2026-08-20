"""
geobridge.semantic.engine
~~~~~~~~~~~~~~~~~~~~~~~~~

Lightweight semantic resolution engine for Copernicus dataset discovery.

Translates user-oriented queries (e.g. "urban heat island", "air quality
exposure") into concrete dataset, variable, access method, and processing
recommendations. Implementation is rule-based — no machine learning, no
large language models. All mappings live in vocabulary.yaml and are
auditable.

Usage
-----
    >>> import geobridge as gb
    >>> matches = gb.semantic_search("urban heat")
    >>> for m in matches:
    ...     print(m.use_case, m.dataset_id, m.recommended_access)
    >>> print(matches[0].guidance)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_VOCABULARY_PATH = Path(__file__).parent / "vocabulary.yaml"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ResourceMatch:
    """A deduplicated dataset+variable pair resolved from a free-text query."""

    dataset_id: str
    variable: str
    confidence: float = 0.0
    themes: list[str] = field(default_factory=list)
    use_cases: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"ResourceMatch(dataset={self.dataset_id!r}, "
            f"variable={self.variable!r}, confidence={self.confidence:.2f})"
        )


@dataclass
class SemanticMatch:
    """A single resolved use case with concrete recommendations."""

    use_case: str
    use_case_label: str
    theme: str
    theme_label: str
    typical_question: str
    dataset_id: str
    variable: str
    recommended_access: str
    recommended_aggregation: str
    recommended_style: str
    typical_aoi_scale: str
    typical_time_window: str
    requires_fusion: bool = False
    confidence: float = 0.0
    matched_terms: list[str] = field(default_factory=list)
    guidance: str = ""

    def __repr__(self) -> str:
        return (
            f"SemanticMatch(use_case={self.use_case!r}, "
            f"dataset={self.dataset_id!r}, variable={self.variable!r}, "
            f"confidence={self.confidence:.2f})"
        )


# ---------------------------------------------------------------------------
# Vocabulary loading
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_vocabulary() -> dict:
    """Load and cache the vocabulary YAML file."""
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "Semantic search requires PyYAML. Install with:\n"
            "    pip install pyyaml"
        ) from exc

    if not _VOCABULARY_PATH.exists():
        raise FileNotFoundError(
            f"Vocabulary file missing at {_VOCABULARY_PATH}. "
            "This indicates a broken installation."
        )
    with _VOCABULARY_PATH.open(encoding="utf-8") as fp:
        return yaml.safe_load(fp)


# ---------------------------------------------------------------------------
# Public listing helpers
# ---------------------------------------------------------------------------

def list_themes() -> list[dict]:
    """
    Return all themes as dicts with id, label, description, and use cases.

    Examples
    --------
    >>> for theme in gb.list_themes():
    ...     print(theme["id"], "—", theme["label"])
    """
    vocab = _load_vocabulary()
    return [
        {
            "id": theme_id,
            "label": data["label"],
            "description": data.get("description", "").strip(),
            "use_cases": data.get("use_cases", []),
            "synonyms": data.get("synonyms", []),
        }
        for theme_id, data in vocab.get("themes", {}).items()
    ]


def list_use_cases(theme: Optional[str] = None) -> list[dict]:
    """
    Return all use cases as dicts. Filter by theme if given.

    Examples
    --------
    >>> for uc in gb.list_use_cases(theme="heat_stress"):
    ...     print(uc["id"], "—", uc["label"])
    """
    vocab = _load_vocabulary()
    use_cases = vocab.get("use_cases", {})
    return [
        {"id": uc_id, **data}
        for uc_id, data in use_cases.items()
        if theme is None or data.get("theme") == theme
    ]


# ---------------------------------------------------------------------------
# Tokenisation and matching
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "the", "a", "an", "of", "for", "in", "on", "at", "to", "and", "or",
    "i", "want", "need", "show", "me", "find", "get", "how", "what", "where",
    "is", "are", "do", "does", "with", "from", "this", "that",
}


_STEM_SUFFIXES = ("ing", "edly", "ed", "es", "s")


def _stem(token: str) -> str:
    """Strip common inflectional suffixes so e.g. 'flooding'/'floods' collapse to 'flood'."""
    for suffix in _STEM_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def _tokenize(query: str) -> list[str]:
    """Lowercase, strip punctuation, drop stopwords, stem."""
    tokens = re.findall(r"[a-zA-Z0-9.]+", query.lower())
    return [_stem(t) for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _fuzzy_equal(a: str, b: str, threshold: float = 0.84) -> bool:
    """True if *a* and *b* are identical or close enough to be a typo of each other."""
    if a == b:
        return True
    # Guard short tokens: fuzzy comparison on 2-3 char strings produces false positives.
    if len(a) < 4 or len(b) < 4:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def _fuzzy_intersect(query_tokens: set[str], term_tokens: set[str]) -> set[str]:
    """Subset of *term_tokens* that fuzzy-match some token in *query_tokens*."""
    return {t for t in term_tokens if any(_fuzzy_equal(q, t) for q in query_tokens)}


def _fuzzy_subset(term_tokens: set[str], query_tokens: set[str]) -> bool:
    """True if every token in *term_tokens* fuzzy-matches some token in *query_tokens*."""
    return bool(term_tokens) and all(
        any(_fuzzy_equal(q, t) for q in query_tokens) for t in term_tokens
    )


def _term_overlap(query_tokens: set[str], term_tokens: set[str]) -> float:
    """Fraction of *term_tokens* fuzzy-present in *query_tokens*."""
    if not term_tokens:
        return 0.0
    return len(_fuzzy_intersect(query_tokens, term_tokens)) / len(term_tokens)


def _score_theme(query_tokens: set[str], theme_id: str, theme_data: dict) -> tuple[float, list[str]]:
    """
    Score how well *query_tokens* matches a theme.

    Returns (score, matched_terms_list).
    """
    matches: list[str] = []
    score = 0.0

    # Direct id match
    if theme_id.replace("_", " ") in " ".join(query_tokens):
        score += 0.5
        matches.append(theme_id)

    # Label words
    label_tokens = set(_tokenize(theme_data.get("label", "")))
    overlap = _term_overlap(query_tokens, label_tokens)
    if overlap > 0:
        score += 0.4 * overlap
        matches.extend(_fuzzy_intersect(query_tokens, label_tokens))

    # Synonyms
    for synonym in theme_data.get("synonyms", []):
        synonym_tokens = set(_tokenize(synonym))
        if _fuzzy_subset(synonym_tokens, query_tokens):
            score += 0.6
            matches.append(synonym)
        else:
            o = _term_overlap(query_tokens, synonym_tokens)
            if o > 0.5:
                score += 0.3 * o
                matches.extend(_fuzzy_intersect(query_tokens, synonym_tokens))

    return score, list(set(matches))


def _detect_combination_intent(raw_query: str) -> bool:
    """Detect if the user is asking about combining multiple variables/datasets."""
    raw = raw_query.lower()
    keywords = ("combined", " and ", " with ", "correlation",
                "joint", "together", "vs ", " versus ")
    return any(kw in raw for kw in keywords)


def _score_use_case(
    query_tokens: set[str],
    use_case_id: str,
    use_case_data: dict,
    theme_score: float,
    has_combination_intent: bool = False,
) -> tuple[float, list[str]]:
    """Score a use case against the query, boosted by its theme score."""
    matches: list[str] = []
    score = theme_score * 0.5  # Inherit half of the theme's score

    # Use case id match
    if use_case_id.replace("_", " ") in " ".join(query_tokens):
        score += 0.6
        matches.append(use_case_id)

    # Label words
    label_tokens = set(_tokenize(use_case_data.get("label", "")))
    overlap = _term_overlap(query_tokens, label_tokens)
    if overlap > 0:
        score += 0.5 * overlap
        matches.extend(_fuzzy_intersect(query_tokens, label_tokens))

    # Variable name match (e.g. "pm2.5", "temperature")
    # Support both new 'variable' (singular) and old 'recommended_variables' (list)
    variables_to_check = use_case_data.get("recommended_variables") or []
    if not variables_to_check and use_case_data.get("variable"):
        variables_to_check = [use_case_data["variable"]]
    for variable in variables_to_check:
        var_tokens = set(_tokenize(variable))
        if _fuzzy_subset(var_tokens, query_tokens):
            score += 0.4
            matches.append(variable)

    # Penalise use cases that require fusion of multiple datasets unless
    # the query contains language explicitly suggesting combination
    # (otherwise a query like "air quality exposure" would inappropriately
    # surface combined-heat-and-pollution above the simpler PM2.5 use case).
    if use_case_data.get("requires_fusion") and not has_combination_intent:
        score *= 0.6

    return score, list(set(matches))


def _resolve_dataset_variable(use_case_data: dict) -> tuple[str | None, str | None]:
    """
    Resolve a use case's dataset and variable.

    New vocabulary uses singular 'dataset' / 'variable' fields. Old
    vocabulary used 'recommended_datasets' / 'recommended_variables' lists.
    Support both for backwards compatibility.
    """
    dataset_id = (
        use_case_data.get("dataset")
        or (use_case_data.get("recommended_datasets") or [None])[0]
    )
    variable = (
        use_case_data.get("variable")
        or (use_case_data.get("recommended_variables") or [None])[0]
    )
    return dataset_id, variable


# ---------------------------------------------------------------------------
# Guidance text builder
# ---------------------------------------------------------------------------

def _build_guidance(use_case_data: dict, dataset_id: str, variable: str,
                    compat_notes: list[str]) -> str:
    """Construct human-readable guidance text for a SemanticMatch."""
    access = use_case_data.get("recommended_access", "api")
    aggregation = use_case_data.get("recommended_aggregation", "raw")
    style = use_case_data.get("recommended_style", "raw")
    aoi_scale = use_case_data.get("typical_aoi_scale", "regional")
    time_window = use_case_data.get("typical_time_window", "user-defined")

    lines = [
        f"Recommended dataset: {dataset_id}",
        f"Recommended variable: {variable}",
        f"Recommended access method: {access}",
        f"Recommended aggregation: {aggregation}",
        f"Recommended style: {style}",
        f"Typical AOI scale: {aoi_scale}",
        f"Typical time window: {time_window}",
    ]
    if compat_notes:
        lines.append("")
        lines.append("Compatibility notes:")
        for note in compat_notes:
            lines.append(f"  • {note}")
    return "\n".join(lines)


def _compatibility_notes_for(dataset_ids: list[str], vocab: dict) -> list[str]:
    """Return relevant compatibility notes for a list of datasets."""
    if len(dataset_ids) < 2:
        return []
    notes: list[str] = []
    for rule in vocab.get("compatibility_rules", []):
        between = set(rule.get("between", []))
        if between.issubset(set(dataset_ids)):
            severity = rule.get("severity", "info").upper()
            notes.append(f"[{severity}] {rule.get('note', '')}")
    return notes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def semantic_search(
    query: str,
    max_results: int = 5,
    min_confidence: float = 0.1,
) -> list[SemanticMatch]:
    """
    Resolve a user-oriented query into concrete dataset and workflow recommendations.

    Parameters
    ----------
    query : str
        Free-text query, e.g. "urban heat island", "air quality exposure",
        "wildfire risk in summer".
    max_results : int
        Maximum number of matches to return (default 5).
    min_confidence : float
        Minimum confidence score (0–1) to include a match (default 0.1).

    Returns
    -------
    list[SemanticMatch]
        Sorted by descending confidence. Each match contains the resolved
        dataset, variable, recommended access method, aggregation, style,
        and human-readable guidance text.

    Examples
    --------
    >>> matches = gb.semantic_search("I want to map urban heat island")
    >>> top = matches[0]
    >>> print(top.dataset_id, top.variable, top.recommended_access)
    >>> print(top.guidance)

    >>> # Combined heat and pollution query — yields fusion-required match
    >>> matches = gb.semantic_search("how does heat correlate with air pollution")
    >>> for m in matches:
    ...     if m.requires_fusion:
    ...         print("Fusion needed:", m.use_case_label)
    """
    if not query.strip():
        return []

    vocab = _load_vocabulary()
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return []

    has_combination = _detect_combination_intent(query)

    themes = vocab.get("themes", {})
    use_cases = vocab.get("use_cases", {})

    # Step 1: score each theme
    theme_scores: dict[str, tuple[float, list[str]]] = {}
    for theme_id, theme_data in themes.items():
        score, matched = _score_theme(query_tokens, theme_id, theme_data)
        theme_scores[theme_id] = (score, matched)

    # Step 2: score each use case
    matches: list[SemanticMatch] = []
    for uc_id, uc_data in use_cases.items():
        theme_id = uc_data.get("theme", "")
        theme_score, theme_matches = theme_scores.get(theme_id, (0.0, []))
        score, uc_matched = _score_use_case(
            query_tokens, uc_id, uc_data, theme_score,
            has_combination_intent=has_combination,
        )

        if score < min_confidence:
            continue

        dataset_id, variable = _resolve_dataset_variable(uc_data)
        if not dataset_id or not variable:
            continue

        # For compatibility notes, collect all datasets this use case touches
        all_datasets = [dataset_id]
        if uc_data.get("fusion_dataset"):
            all_datasets.append(uc_data["fusion_dataset"])
        compat_notes = _compatibility_notes_for(all_datasets, vocab)
        guidance = _build_guidance(uc_data, dataset_id, variable, compat_notes)

        all_matched = list(set(theme_matches + uc_matched))

        matches.append(SemanticMatch(
            use_case=uc_id,
            use_case_label=uc_data.get("label", uc_id),
            theme=theme_id,
            theme_label=themes.get(theme_id, {}).get("label", theme_id),
            typical_question=uc_data.get("typical_question", ""),
            dataset_id=dataset_id,
            variable=variable,
            recommended_access=uc_data.get("recommended_access", "api"),
            recommended_aggregation=uc_data.get("recommended_aggregation", "raw"),
            recommended_style=uc_data.get("recommended_style", "raw"),
            typical_aoi_scale=uc_data.get("typical_aoi_scale", "regional"),
            typical_time_window=uc_data.get("typical_time_window", ""),
            requires_fusion=uc_data.get("requires_fusion", False),
            confidence=min(score, 1.0),
            matched_terms=all_matched,
            guidance=guidance,
        ))

    matches.sort(key=lambda m: m.confidence, reverse=True)
    return matches[:max_results]


def semantic_resources(
    query: str,
    max_results: int = 10,
    min_confidence: float = 0.1,
) -> list[ResourceMatch]:
    """
    Resolve a free-text query into a ranked list of datasets and variables.

    Combines two retrieval signals so neither's blind spot dominates:

    - **Rule-based** (:func:`semantic_search`, fuzzy token/synonym matching
      over the curated ``vocabulary.yaml`` taxonomy). Reliable for phrasing
      curators anticipated — including paraphrases with zero literal overlap
      with a dataset's own description (e.g. "urban heat island" resolving
      to a temperature dataset whose text never says "heat" or "urban").
    - **TF-IDF catalog retrieval** (:mod:`geobridge.semantic.catalog`, cosine
      similarity over every dataset/subset/variable in the full ARCO
      catalogue — not just the ~40 curated use cases). Extends coverage to
      resources no curator has written a use case for, at the cost of no
      synonym knowledge: it only sees literal shared vocabulary.

    A (dataset_id, variable) pair's final confidence is the stronger of the
    two signals; pairs found by both keep their curated themes/use_cases.
    Pairs found only by TF-IDF are still returned, with empty themes/use_cases.

    Parameters
    ----------
    query : str
        Free-text query, e.g. "temperature and air quality over cities".
    max_results : int
        Maximum number of (dataset, variable) pairs to return (default 10).
    min_confidence : float
        Minimum confidence to include a pair (default 0.1).

    Returns
    -------
    list[ResourceMatch]
        Sorted by descending confidence. Each entry carries the dataset id,
        variable name, confidence, and any curated themes/use_cases that
        happen to match — empty lists when no curated use case exists for
        that resource.

    Examples
    --------
    >>> resources = gb.semantic_resources("urban heat and air pollution")
    >>> for r in resources:
    ...     print(r.dataset_id, r.variable, r.confidence)
    """
    if not query.strip():
        return []

    from geobridge.semantic import catalog
    ranked = catalog.query_catalog(query, top_k=max_results * 3)
    tfidf_scores = {(dataset_id, variable): score for dataset_id, variable, score in ranked}

    use_case_matches = semantic_search(query, max_results=max_results * 3, min_confidence=min_confidence)

    combined: dict[tuple[str, str], ResourceMatch] = {}

    for m in use_case_matches:
        key = (m.dataset_id, m.variable)
        confidence = max(m.confidence, tfidf_scores.pop(key, 0.0))
        if key in combined:
            existing = combined[key]
            existing.confidence = max(existing.confidence, confidence)
            if m.theme not in existing.themes:
                existing.themes.append(m.theme)
            if m.use_case not in existing.use_cases:
                existing.use_cases.append(m.use_case)
        else:
            combined[key] = ResourceMatch(
                dataset_id=m.dataset_id,
                variable=m.variable,
                confidence=confidence,
                themes=[m.theme],
                use_cases=[m.use_case],
            )

    # TF-IDF-only matches — extends coverage beyond the curated taxonomy.
    for (dataset_id, variable), score in tfidf_scores.items():
        combined[(dataset_id, variable)] = ResourceMatch(
            dataset_id=dataset_id,
            variable=variable,
            confidence=score,
            themes=[],
            use_cases=[],
        )

    results = [r for r in combined.values() if r.confidence >= min_confidence]

    # Normalise so the top score is 1.0
    if results:
        max_conf = max(r.confidence for r in results)
        if max_conf > 0:
            for r in results:
                r.confidence = round(r.confidence / max_conf, 4)

    results.sort(key=lambda r: r.confidence, reverse=True)
    return results[:max_results]
