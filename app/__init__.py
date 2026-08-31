"""Attacker package metadata."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("attacker")
except PackageNotFoundError:
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
