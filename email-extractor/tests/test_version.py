"""Version drift guard — the add-on version (config.yaml) must match the app
__version__ shown on the dashboard / /health. Drift means the dashboard lies
about what is deployed (0.2.4 shown while add-on 0.3.1 ran, 2026-07-14)."""

import re
from pathlib import Path

from app import __version__

CONFIG_YAML = Path(__file__).resolve().parents[1] / "config.yaml"


def _addon_version() -> str:
    m = re.search(r'^version:\s*"?([^"\s]+)"?\s*$', CONFIG_YAML.read_text(), re.M)
    assert m, "config.yaml has no version line"
    return m.group(1)


def test_app_version_matches_addon_version():
    assert __version__ == _addon_version()
