"""kiln: a content-addressed cache for build outputs."""

from kiln.fingerprint import fingerprint
from kiln.manifest import ManifestError, load
from kiln.store import Store

__all__ = ["ManifestError", "Store", "fingerprint", "load"]
__version__ = "0.2.0"
