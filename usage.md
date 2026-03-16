Usage (no commit hashes anywhere):
  asv initialize_diffcheck --source-root ./src/mypackage
  # → writes .lightspeed_deps.db (benchmark_dep + benchmark_meta + baseline)

  asv measure_impacted --changed-files src/mypackage/foo.py
  # → runs only affected benchmarks, prints delta table

  asv measure_impacted --from-git-diff --factor 1.05
  # → fails if any benchmark regresses >5%