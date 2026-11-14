"""Domain errors for lakeinlet.

The public CLI converts these errors into a concise message and exit code 2.
They are deliberately small so callers can distinguish user/configuration
problems from unexpected programming defects.
"""


class LakeInletError(Exception):
    """Base class for expected lakeinlet failures."""


class ConfigError(LakeInletError):
    """The YAML configuration is missing or invalid."""


class ManifestError(LakeInletError):
    """A manifest cannot be read or does not satisfy the manifest schema."""


class LedgerError(LakeInletError):
    """The SQLite ledger is missing, unreadable, or has an incompatible schema."""


class ServeError(LakeInletError):
    """The local viewer was asked to start in an unsafe configuration."""
