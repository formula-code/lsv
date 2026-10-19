"""A changed file that is not source (a cProfile dump) is hashed, not parsed (rich#3546 trials left .prof files)."""

from asv.contrib.lightspeed.fingerprint import changed_files_with_fingerprints


def test_binary_changed_file_does_not_stop_fingerprinting(tmp_path):
    prof = tmp_path / "align_profile.prof"
    prof.write_bytes(b"\xfb\x00\x80\x9c" * 64)
    src = tmp_path / "cells.py"
    src.write_text("def cell_len(s):\n    return len(s)\n")
    out = changed_files_with_fingerprints([str(prof), str(src)], {})
    assert set(out) == {str(prof), str(src)}
    assert out[str(src)][0]
