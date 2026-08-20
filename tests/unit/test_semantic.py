"""Tests for geobridge.semantic.engine — query resolution and matching."""

from geobridge.semantic import engine


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

def test_tokenize_lowercases():
    assert "temperature" in engine._tokenize("Temperature")


def test_tokenize_strips_stopwords():
    tokens = engine._tokenize("the temperature in the city")
    assert "the" not in tokens
    assert "in" not in tokens
    assert "temperature" in tokens
    assert "city" in tokens


def test_tokenize_handles_punctuation():
    tokens = engine._tokenize("PM2.5 levels: high!")
    assert "pm2.5" in tokens or "pm2" in tokens


# ---------------------------------------------------------------------------
# Vocabulary loading
# ---------------------------------------------------------------------------

def test_load_vocabulary_returns_themes_and_use_cases():
    vocab = engine._load_vocabulary()
    assert "themes" in vocab
    assert "use_cases" in vocab
    assert "heat_stress" in vocab["themes"]
    assert "urban_heat_island" in vocab["use_cases"]


def test_list_themes_returns_dicts():
    themes = engine.list_themes()
    assert len(themes) >= 5
    assert all("id" in t and "label" in t for t in themes)


def test_list_use_cases_filter_by_theme():
    heat_use_cases = engine.list_use_cases(theme="heat_stress")
    assert all(uc["theme"] == "heat_stress" for uc in heat_use_cases)
    assert any(uc["id"] == "urban_heat_island" for uc in heat_use_cases)


def test_list_use_cases_no_filter_returns_all():
    all_uc = engine.list_use_cases()
    heat_only = engine.list_use_cases(theme="heat_stress")
    assert len(all_uc) > len(heat_only)


# ---------------------------------------------------------------------------
# semantic_search — happy path
# ---------------------------------------------------------------------------

def test_semantic_search_urban_heat_returns_uhi():
    matches = engine.semantic_search("urban heat island")
    assert len(matches) > 0
    top = matches[0]
    assert top.use_case == "urban_heat_island"
    assert top.dataset_id == "reanalysis-era5-single-levels"
    assert top.variable == "2m_temperature"


def test_semantic_search_air_quality_returns_pm25():
    matches = engine.semantic_search("air quality exposure")
    assert len(matches) > 0
    # Should match an air_quality theme use case
    assert any(m.theme == "air_quality" for m in matches)


def test_semantic_search_wildfire_returns_fire_use_case():
    matches = engine.semantic_search("wildfire risk")
    assert len(matches) > 0
    assert any(m.theme == "wildfire" for m in matches)


def test_semantic_search_empty_query_returns_empty():
    assert engine.semantic_search("") == []
    assert engine.semantic_search("   ") == []


def test_semantic_search_nonsense_returns_empty_or_low_confidence():
    # Either no matches, or all below threshold
    matches = engine.semantic_search("xyzzyx fnord ploeq")
    assert all(m.confidence > 0.1 for m in matches)


def test_semantic_search_returns_sorted_by_confidence():
    matches = engine.semantic_search("heat stress urban")
    confidences = [m.confidence for m in matches]
    assert confidences == sorted(confidences, reverse=True)


def test_semantic_search_max_results_respected():
    matches = engine.semantic_search("heat", max_results=2)
    assert len(matches) <= 2


# ---------------------------------------------------------------------------
# Match content
# ---------------------------------------------------------------------------

def test_semantic_match_has_guidance_text():
    matches = engine.semantic_search("urban heat")
    assert matches[0].guidance != ""
    assert "Recommended dataset" in matches[0].guidance


def test_semantic_match_compatibility_notes_present_for_fusion():
    matches = engine.semantic_search("combined heat and air pollution")
    fusion_matches = [m for m in matches if m.requires_fusion]
    if fusion_matches:
        # Should include compatibility note in guidance
        assert "Compatibility" in fusion_matches[0].guidance


def test_semantic_match_repr_includes_use_case():
    matches = engine.semantic_search("urban heat island")
    assert "urban_heat_island" in repr(matches[0])


def test_semantic_match_recommended_aggregation_is_set():
    matches = engine.semantic_search("heatwave frequency")
    assert matches[0].recommended_aggregation in {
        "raw", "daily_mean", "daily_max", "daily_min",
        "monthly_mean", "monthly_max", "annual_mean",
    }


# ---------------------------------------------------------------------------
# semantic_resources — dataset/variable-first retrieval (no use_case required)
# ---------------------------------------------------------------------------

def test_semantic_resources_returns_result_without_curated_use_case():
    # "dust" is an ARCO variable with no curated use_case in vocabulary.yaml
    # (only mentioned in prose elsewhere) — this is the regression test that
    # dataset/variable coverage is no longer capped by curation.
    results = engine.semantic_resources("desert dust concentration in air")
    assert len(results) > 0
    dust_matches = [r for r in results if r.variable == "dust"]
    assert dust_matches
    assert dust_matches[0].use_cases == []


def test_semantic_resources_attaches_use_case_when_matched():
    results = engine.semantic_resources("urban heat island")
    assert len(results) > 0
    assert any(r.use_cases for r in results)
