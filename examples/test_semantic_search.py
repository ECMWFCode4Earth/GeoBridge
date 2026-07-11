import os
import geobridge as gb

def main():
    # Authenticate with the geobridge service
    print("\n[1/4] Authenticating...")
    gb.authenticate()

    # 2. Resolve the user's intent via semantic search
    print("\n[2/4] Resolving 'I want to study temperature' via semantic search...")
    matches = gb.semantic_search("moisture")
    top = matches[0]
    print(f"  Top match: {top.use_case_label}")
    print(f"  Dataset:   {top.dataset_id}")
    print(f"  Variable:  {top.variable}")
    print(f"  Aggregation: {top.recommended_aggregation}")          

    
    print("\n")

if __name__ == '__main__':
    main()
