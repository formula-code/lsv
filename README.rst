lightspeed velocity
===================

This repository is a fork of `airspeed velocity (asv) <https://github.com/airspeed-velocity/asv>`_ with one extension, Lightspeed. Lightspeed records which source files each benchmark executes, and then re-runs only the benchmarks that a set of changed files can affect. It adds two commands, ``asv initialize_diffcheck`` and ``asv measure_impacted``, and a Python API in ``asv/contrib/lightspeed/``. The rest of asv works as upstream.

See `lightspeed_docs.md <lightspeed_docs.md>`_ for how Lightspeed works, its options and its Python API.
