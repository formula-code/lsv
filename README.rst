lightspeed velocity
===================

This repository is a fork of `airspeed velocity (asv) <https://github.com/airspeed-velocity/asv>`_ with one extension, Lightspeed. Lightspeed records which source files each benchmark executes, and then re-runs only the benchmarks that a set of changed files can affect. The command is ``lsv``. It adds two subcommands, ``lsv initialize_diffcheck`` and ``lsv measure_impacted``, and a Python API in ``asv/contrib/lightspeed/``. The rest of asv works as upstream.

See `lightspeed_docs.md <lightspeed_docs.md>`_ for how Lightspeed works, its options and its Python API.
