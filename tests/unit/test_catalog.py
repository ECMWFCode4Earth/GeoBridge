"""Tests for geobridge.semantic.catalog — TF-IDF dataset/variable retrieval."""

from geobridge.semantic import catalog

# ---------------------------------------------------------------------------
# Variable aliases
# ---------------------------------------------------------------------------

def test_load_variable_aliases_inverts_short_to_long():
    aliases = catalog._load_variable_aliases()
    assert "t2m" in aliases
    assert "2m temperature" in aliases["t2m"]


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

def test_build_corpus_entries_nonempty():
    entries = catalog._build_corpus_entries()
    assert len(entries) > 0
    assert all(e.dataset_id and e.variable and e.text for e in entries)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def test_query_catalog_temperature_surfaces_t2m():
    results = catalog.query_catalog("temperature", top_k=15)
    assert len(results) > 0
    assert any(variable == "t2m" for _, variable, _ in results)


def test_query_catalog_alias_matches_short_form():
    results = catalog.query_catalog("2m temperature", top_k=15)
    assert any(variable == "t2m" for _, variable, _ in results)


def test_query_catalog_scores_sorted_descending():
    results = catalog.query_catalog("air pollution particulate matter", top_k=15)
    scores = [score for _, _, score in results]
    assert scores == sorted(scores, reverse=True)


def test_query_catalog_empty_for_no_match():
    results = catalog.query_catalog("zzznonsensequery123", top_k=15)
    assert results == []


def test_query_catalog_deduplicates_dataset_variable_pairs():
    results = catalog.query_catalog("temperature", top_k=50)
    pairs = [(d, v) for d, v, _ in results]
    assert len(pairs) == len(set(pairs))
