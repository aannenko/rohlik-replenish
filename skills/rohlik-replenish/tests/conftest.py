import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_from_the_live_session(tmp_path_factory, monkeypatch):
    """The OS keyring and the default data directory may hold a real user's live session, and a command that found
    it would call Rohlik. No test may reach them: each test gets its own data directory, an in-memory keyring and no
    browser. Tests that need their own replace these again."""
    import webbrowser

    import keyring

    from engine.mcp_transport import MemoryKeyring
    from engine.paths import DATA_DIR_ENV

    memory = MemoryKeyring()
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path_factory.mktemp("data")))
    monkeypatch.setattr(keyring, "get_password", memory.get_password)
    monkeypatch.setattr(keyring, "set_password", memory.set_password)
    monkeypatch.setattr(keyring, "delete_password", memory.delete_password)

    def no_browser(*_args, **_kwargs):
        raise AssertionError("a test tried to open a browser")

    monkeypatch.setattr(webbrowser, "open", no_browser)


@pytest.fixture(scope="session")
def synthetic_fixtures(tmp_path_factory):
    """The synthetic household's `--fixtures` directory, with its tags.json (tests/synthetic.py). Shared: read only."""
    from tests.synthetic import write

    return write(tmp_path_factory.mktemp("synthetic"))
