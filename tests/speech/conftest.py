"""
Pytest configuration for speech tests.

Always regenerates smoke tests from patterns.toml before running tests.
"""

import sys
from pathlib import Path


def pytest_configure(config):
    """Regenerate smoke tests from patterns.toml."""
    project_root = Path(__file__).parent.parent.parent

    # Ensure project root is in path for imports
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # wh-z69w: the service modules use bare intra-service imports
    # (from utils..., from ai...) that resolve only with the service
    # directory itself on sys.path, alongside the package-style
    # services.wheelhouse... imports the test files here use.
    service_dir = project_root / "services" / "wheelhouse"
    if str(service_dir) not in sys.path:
        sys.path.insert(1, str(service_dir))

    from tests.speech.generate_smoke_tests import generate_smoke_tests
    generate_smoke_tests()
