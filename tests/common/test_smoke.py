import importlib


def test_packages_import() -> None:
    for name in ("brierlib", "recorder", "study"):
        assert importlib.import_module(name).__name__ == name
