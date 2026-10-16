"""Версия диагностики совпадает с фактически установленным пакетом и сборкой."""
from importlib.metadata import version
from pathlib import Path
import tomllib

from atlas_gate import __version__
from .test_discovery import configured


def test_version_matches_installed_and_declared_metadata():
    root = Path(__file__).parents[1]
    assert __version__ == version('atlas-gate')
    assert tomllib.loads((root / 'pyproject.toml').read_text())['project']['version'] == __version__
    locked = tomllib.loads((root / 'uv.lock').read_text())
    assert next(p for p in locked['package'] if p['name'] == 'atlas-gate')['version'] == __version__


async def test_health_reports_installed_package_version(configured):
    client, state, _ = configured
    response = await client.get('/harness/health')
    assert response.status_code == 200
    assert response.json()['version'] == version('atlas-gate') == __version__
