# These are manual, live-gateway smoke/cache scripts (run explicitly with
# `python tests/manual/<name>.py` against a deployed environment), NOT unit
# tests. Several end in `_test.py`, which pytest would otherwise auto-collect
# and fail on (they need a live gateway + a virtual key from a local .env).
# Exclude the whole folder from collection so `pytest` stays green offline.
collect_ignore_glob = ["*"]
