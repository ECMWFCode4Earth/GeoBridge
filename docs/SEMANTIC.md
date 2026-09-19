# GeoBridge Semantic Search — Reference Notes

Source: `geobridge/semantic/engine.py` and `geobridge/semantic/catalog.py`.
Last reviewed against commit `c0640a6`. See also
[DISCOVERY.md](DISCOVERY.md) (the ARCO snapshot `catalog.py` indexes).

## 1. What problem this solves

Both modules answer the same kind of question — "I want to look at [urban
heat / air quality / flood risk], what dataset and variable should I
use?" — from a free-text query, without the user needing to already know
Copernicus dataset ids or ARCO variable short names. There are **two
independent retrieval engines**, combined by one public function:

| Engine | Module | Matches against | Strength | Blind spot |
|---|---|---|---|---|
| Rule-based / curated | `engine.py` (`semantic_search`) | ~40 hand-written use cases in `vocabulary.yaml` | Understands paraphrases and synonyms a curator anticipated (e.g. "urban heat island" → a temperature dataset whose own text never says "heat") | Only covers datasets/phrasings a curator has written a use case for |
| TF-IDF catalog retrieval | `catalog.py` (`query_catalog`) | Every (dataset, subset, variable) triple in `arco_snapshot.yaml` — 1000+ short documents | Covers the entire ARCO catalogue, no curation lag | No synonym knowledge — only sees literal shared vocabulary between query and dataset text |

`semantic_resources()` (in `engine.py`) runs both and merges the results —
see §5.

Both are **rule-based / classical IR** — no ML models, no embeddings, no
network calls. Everything is deterministic and auditable by reading
`vocabulary.yaml` or the ARCO snapshot directly.

## 2. `engine.py` — curated rule-based matching

### Data source: `vocabulary.yaml`

A hand-maintained YAML file (`geobridge/semantic/vocabulary.yaml`) with
three top-level sections:

- **`themes`** — broad categories (e.g. `heat_stress`,
  `air_quality`), each with a `label`, `description`, a list of
  `synonyms` (free-text phrases that should trigger the theme), and the
  `use_cases` that belong to it.
- **`use_cases`** — concrete, resolvable queries (e.g.
  `urban_heat_island`, `utci_exposure`) each mapping to one
  `dataset`/`variable` pair (ARCO-convention: underscored dataset id,
  short variable name — see the file's own header comment), plus
  `recommended_access`, `recommended_aggregation`, `recommended_style`,
  `typical_aoi_scale`, `typical_time_window`, and an optional
  `requires_fusion` + `fusion_dataset` when the use case genuinely needs
  two datasets combined (via `gb.fuse()`).
- **`compatibility_rules`** — pairwise notes between datasets (e.g. "ERA5
  is 0.25° native, CAMS Europe is 0.1° — resample before fusing") with a
  `severity` (`info`/`warning`/etc.), surfaced automatically in guidance
  text when a match touches more than one dataset.

Loaded once via `_load_vocabulary()` (`@lru_cache`); raises
`FileNotFoundError` if missing (treated as a broken install, not a normal
runtime condition) and a clear `RuntimeError` if PyYAML isn't installed.

`_resolve_dataset_variable()` accepts both the current singular
`dataset`/`variable` fields and an older `recommended_datasets`/
`recommended_variables` list form, for backwards compatibility with older
vocabulary file versions.

### Tokenisation and fuzzy matching

`_tokenize(query)`: lowercases, extracts `[a-zA-Z0-9.]+` runs (so `pm2.5`
survives as one token), drops a small stopword list (`the`, `want`, `show`,
`how`, etc. — task/question scaffolding words, not domain terms), and
stems each token by stripping common inflectional suffixes
(`_STEM_SUFFIXES = ("ing", "edly", "ed", "es", "s")`, only applied when at
least 3 characters remain — so "flooding"/"floods" both collapse to
"flood", but "as" doesn't get mangled to "").

`_fuzzy_equal(a, b, threshold=0.84)`: exact match, or
`difflib.SequenceMatcher` ratio ≥ 0.84 — but only for tokens ≥ 4 characters
(shorter strings produce too many false-positive fuzzy matches, e.g. "co"
vs "no"). This is what lets a query with a typo or slightly different
inflection still match a vocabulary term.

`_fuzzy_intersect` / `_fuzzy_subset` / `_term_overlap` build on
`_fuzzy_equal` to compute, respectively: which term-tokens fuzzy-match the
query, whether *every* term-token fuzzy-matches something in the query
(used for synonym phrases — an "all words present" requirement), and what
fraction of a term's tokens are covered by the query (a soft overlap
score, not a hard requirement).

### Scoring

Two-stage: score every **theme**, then score every **use case**
(inheriting half its theme's score) — `_score_theme` and `_score_use_case`.
Both accumulate a float score from several independent signals rather than
being a single similarity metric:

- **`_score_theme`**: direct theme-id substring match (+0.5), label-word
  overlap (+0.4 × overlap fraction), synonym phrase — full fuzzy subset
  match (+0.6) or partial overlap > 0.5 (+0.3 × overlap).
- **`_score_use_case`**: inherits `theme_score * 0.5`, plus direct
  use-case-id substring match (+0.6), label-word overlap (+0.5 ×
  overlap), variable-name fuzzy match (+0.4). Then, **if the use case
  requires fusing two datasets and the query shows no combination intent**
  (see below), the whole score is multiplied by 0.6 — this stops a simple
  single-variable query like "air quality exposure" from being outranked
  by a fusion use case ("heat + pollution combined") just because it
  shares some vocabulary.

`_detect_combination_intent(raw_query)` is a simple substring check on the
**raw, untokenised** query for phrases like `" and "`, `" with "`,
`"combined"`, `"correlation"`, `"joint"`, `"together"`, `"vs "`,
`" versus "` — deliberately checked against the raw string (not tokens)
so multi-word connective phrases aren't lost to tokenisation.

### `semantic_search(query, max_results=5, min_confidence=0.1) -> list[SemanticMatch]`

Ties it together: tokenise the query, score every theme, score every use
case (skipping anything below `min_confidence`), resolve each surviving
use case's dataset/variable, attach compatibility notes
(`_compatibility_notes_for`, only computed when a use case touches ≥ 2
datasets), build human-readable `guidance` text (`_build_guidance` — one
line per recommendation field, plus a bulleted compatibility-notes
section when present), and return results sorted by descending
`confidence` (capped at 1.0), truncated to `max_results`.

`SemanticMatch` carries: `use_case`, `use_case_label`, `theme`,
`theme_label`, `typical_question`, `dataset_id`, `variable`,
`recommended_access`, `recommended_aggregation`, `recommended_style`,
`typical_aoi_scale`, `typical_time_window`, `requires_fusion`,
`confidence`, `matched_terms`, `guidance`.

```python
import geobridge as gb

matches = gb.semantic_search("I want to map urban heat island")
top = matches[0]
print(top.dataset_id, top.variable, top.recommended_access)
print(top.guidance)
```

### `list_themes()` / `list_use_cases(theme=None)`

Thin read-only accessors over the loaded vocabulary — useful for building
a picker UI or just exploring what's curated, without running any query.

## 3. `catalog.py` — TF-IDF retrieval over the full ARCO catalogue

Where `engine.py` only "sees" what a curator wrote into ~40 use cases,
`catalog.py`'s `query_catalog()` builds a search index over **every**
(dataset, subset, variable) triple in `arco_snapshot.yaml` — so a query
can resolve to a concrete dataset/variable even for combinations no
curator has documented.

### Corpus construction (`_build_corpus_entries`)

For each dataset → each subset → each variable, one `CorpusEntry
(dataset_id, variable, text)` is built by concatenating: dataset title,
dataset description, subset title, the variable's display name and
`standard_name`, the variable's own short name with underscores turned
into spaces, and any CDS long-form aliases for that variable (from
`arco_overrides.yaml`, inverted short→long by `_load_variable_aliases`).
That alias step matters: it's what lets a query phrased with the CDS long
name ("2m temperature") still match a corpus entry built around the ARCO
short name ("t2m"), since the short name alone wouldn't share vocabulary
with the query.

### TF-IDF, implemented in pure Python

No `scikit-learn`/`gensim` dependency — the corpus is small (~1000 short
strings), so `_TfidfModel` implements the standard textbook formula
directly:

- **Tokenisation** (`_analyze`): lowercase, extract `\b\w\w+\b` tokens
  (2+ word characters), drop a stopword list (function words like "the",
  "and", "with" — smaller and more generic than `engine.py`'s, since this
  index has no task-phrasing to filter out), then emit both **unigrams
  and bigrams** (adjacent-token pairs) — bigrams let a two-word phrase
  like "sea surface" score higher for documents where those words appear
  adjacently, not just separately.
- **IDF**: smoothed inverse document frequency,
  `ln((1 + N) / (1 + df)) + 1` — the classic scikit-learn-style smoothing
  that avoids a divide-by-zero / undefined log for terms in every
  document, and keeps every term's weight strictly positive.
- **TF·IDF vectors**: raw term count × IDF per document, then **L2
  normalised** — which is what makes cosine similarity reduce to a plain
  dot product at query time (`_TfidfModel.similarities`), avoiding a norm
  computation per comparison.
- Query vectors are built the same way (`transform_query`), with any term
  absent from the fitted vocabulary simply dropped (out-of-vocabulary
  terms contribute nothing, rather than erroring).

The whole index (`entries`, fitted `_TfidfModel`) is built once and cached
via `@lru_cache(maxsize=1)` on `_load_index()`.

### `query_catalog(query, top_k=15) -> list[tuple[dataset_id, variable, score]]`

Scores the query against every corpus entry, keeps the **best score per
(dataset_id, variable) pair** (since a dataset can have the same variable
listed under conceptually distinct subsets/entries), drops non-positive
scores, sorts descending, returns up to `top_k`.

## 4. Why two engines instead of one

- The rule-based engine can match queries with **zero literal word
  overlap** with the underlying data (that's the entire point of curating
  synonyms) but its coverage is bounded by how many use cases have been
  written.
- The TF-IDF engine has **complete coverage** of the ARCO catalogue
  automatically, with no curation effort, but it's a bag-of-words method —
  it cannot know that "urban heat island" relates to `t2m` unless that
  exact vocabulary appears in the dataset/variable's own text.

Combining them (§5) means a query gets curated-quality synonym
understanding where it exists, and falls back to literal-overlap coverage
of the *entire* catalogue where it doesn't — rather than the whole
semantic-search feature silently failing outside the curated 40 use cases.

## 5. `semantic_resources(query, max_results=10, min_confidence=0.1) -> list[ResourceMatch]`

The public merge point (lives in `engine.py`, imports `catalog` lazily to
avoid a module-load-order dependency):

1. Runs `catalog.query_catalog(query, top_k=max_results*3)` → TF-IDF
   scores keyed by `(dataset_id, variable)`.
2. Runs `semantic_search(query, max_results=max_results*3, min_confidence=...)`
   → curated matches.
3. For every curated match, confidence becomes
   `max(curated_confidence, tfidf_score_for_same_pair)` — the curated
   result **absorbs** the TF-IDF score for the same pair if TF-IDF scored
   it higher (and removes that pair from further TF-IDF-only
   consideration via `.pop()`), while keeping its `themes`/`use_cases`
   provenance. If the same `(dataset_id, variable)` pair is produced by
   more than one curated use case, their theme/use_case lists are merged
   and the confidence is the max across all of them.
4. Any `(dataset_id, variable)` pair left over in the TF-IDF results
   (i.e. never matched by any curated use case) is still included, with
   empty `themes`/`use_cases` — this is the pure catalogue-coverage
   extension described in §1.
5. Filters by `min_confidence`, then **re-normalises confidences so the
   top result is exactly 1.0** — because the two source scores live on
   different scales (curated scores are an ad-hoc accumulated sum capped
   at 1.0; TF-IDF scores are cosine similarities, typically well below
   1.0) and mixing them un-normalised would make curated matches look
   disproportionately more confident just from source, not fit.

`ResourceMatch` carries: `dataset_id`, `variable`, `confidence`, `themes`,
`use_cases` (both possibly empty, for a TF-IDF-only result).

```python
import geobridge as gb

resources = gb.semantic_resources("urban heat and air pollution")
for r in resources:
    print(r.dataset_id, r.variable, r.confidence, r.themes)
```

## 6. Authentication and network behaviour

Neither module makes any network call or requires `gb.authenticate()` —
both operate entirely over bundled/local YAML (`vocabulary.yaml`,
`arco_snapshot.yaml`, `arco_overrides.yaml`), same offline-by-default
philosophy as `discover.py`.

## 7. Relationship to the other modules

- `catalog.py` reads the ARCO snapshot through
  `discover._load_arco_snapshot()` — the same cached loader
  [DISCOVERY.md](DISCOVERY.md) describes — and the same
  `arco_overrides.yaml` variable-alias file `extract.py` and `wmts.py`
  use, so a dataset/variable resolved here always lines up with what
  `zarr_to_geotiff()` / `wmts_layer()` would accept.
- Neither module resolves a dataset/variable pair all the way to a usable
  URL or tile layer itself — the natural next step after
  `semantic_search()`/`semantic_resources()` is to feed the resolved
  `dataset_id`/`variable` into `gb.discover_one()`, `gb.zarr_to_geotiff()`,
  or `gb.wmts_layer()`.
