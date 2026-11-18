"""Polaris local engine.

``__version__`` is the release this engine was shipped with. The desktop
packaging step (``src/desktop/scripts/stage-backend.mjs``) writes
``app/_release.py`` next to this file with the version taken from the release
tag; ``/api/health``, the data export and the in-app feedback link all report
it. A source checkout has no such file and says so, rather than claiming a
version number it does not have.
"""

try:
    from app._release import VERSION as __version__
except ImportError:  # running from a source checkout
    __version__ = "dev"
