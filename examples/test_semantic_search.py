import os
import geobridge as gb

def main():
    # # Authenticate with the geobridge service
    # print("\n[1/6] Authenticating...")
    # gb.authenticate()

    # 2. Resolve the user's intent via semantic search (curated use cases only)
    print("\n[2/6] Resolving 'fire forecast' via semantic_search()...")
    matches = gb.semantic_search("fire forecast")
    top = matches[0]
    print(f"  Top match: {top.use_case_label}")
    print(f"  Dataset:   {top.dataset_id}")
    print(f"  Variable:  {top.variable}")
    print(f"  Aggregation: {top.recommended_aggregation}")

    # 3. semantic_resources() on a curated query — TF-IDF and the rule-based
    #    curated signal agree, so the result carries attached use_cases.
    print("\n[3/6] Resolving 'urban heat island' via semantic_resources()...")
    for r in gb.semantic_resources("urban heat island", max_results=3):
        print(f"  {r.dataset_id:35s} {r.variable:12s} conf={r.confidence:.2f} use_cases={r.use_cases}")

    # 4. semantic_resources() on a paraphrase with near-zero literal overlap
    #    with any dataset description. TF-IDF alone finds nothing here — this
    #    only resolves because the rule-based signal fuzzy-matches "hot" and
    #    is unioned in as a confidence floor.
    print("\n[4/6] Resolving 'how hot does my city get in summer' via semantic_resources()...")
    resources = gb.semantic_resources("how hot does my city get in summer", max_results=3)
    if resources:
        for r in resources:
            print(f"  {r.dataset_id:35s} {r.variable:12s} conf={r.confidence:.2f} use_cases={r.use_cases}")
    else:
        print("  No matches (rule-based signal found nothing at this confidence threshold).")

    # 5. semantic_resources() on a query with no curated use case at all.
    #    "dust" has no entry anywhere in vocabulary.yaml — this result is
    #    only reachable via TF-IDF retrieval over the full ARCO catalogue,
    #    which is the coverage this function adds beyond curation. Note the
    #    empty use_cases list: no curated guidance exists for these resources.
    print("\n[5/6] Resolving 'mineral dust storm forecast' via semantic_resources()...")
    for r in gb.semantic_resources("mineral dust storm forecast", max_results=3):
        tag = "curated" if r.use_cases else "catalog-only (no curated use case)"
        print(f"  {r.dataset_id:35s} {r.variable:12s} conf={r.confidence:.2f} [{tag}]")

    # 6. semantic_resources() works even without scikit-learn installed — it
    #    degrades gracefully to the rule-based signal alone rather than
    #    raising, so this call is safe regardless of the optional
    #    geobridge[semantic-ml] extra being present.
    print("\n[6/6] Resolving 'air quality exposure' via semantic_resources() "
          "(works with or without the optional scikit-learn dependency)...")
    for r in gb.semantic_resources("air quality exposure", max_results=3):
        print(f"  {r.dataset_id:35s} {r.variable:12s} conf={r.confidence:.2f} use_cases={r.use_cases}")

    print("\n[7/6] My example via semantic_resources() ")
    resources = gb.semantic_resources("ice", max_results=10)
    for r in resources:
        print(r.dataset_id, r.variable, r.confidence)



    print("\n")

if __name__ == '__main__':
    main()
