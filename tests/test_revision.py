from indexops.revision import STARTUP


def test_startup_revision_is_captured_once_with_required_keys():
    assert {"commit", "dirty", "captured_at"} <= set(STARTUP)
    assert STARTUP["dirty"] in (True, False, None)
