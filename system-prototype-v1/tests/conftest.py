import os


def pytest_configure(config):
    """Keep pytest's numbered temporary directories inside this project."""
    if config.getoption("basetemp") or "PYTEST_DEBUG_TEMPROOT" in os.environ:
        return
    temp_root = config.rootpath / "artifacts" / "test-results" / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(temp_root)
