"""kiln: a content-addressed cache for build outputs."""

from kiln.manifest import ManifestError, load
from kiln.store import Store

__all__ = ["ManifestError", "Store", "load"]
__version__ = "0.1.0"
