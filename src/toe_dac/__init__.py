"""TD Agent — a persistent TOE-DAC agent CLI."""

from importlib.metadata import PackageNotFoundError, version

from .service import TDService
from .states import TDState
from .storage import TDRepository

try:
    __version__ = version("toe-dac")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.2.0"

__all__ = ["TDRepository", "TDService", "TDState", "__version__"]
