"""Run a PrivacyFence data-sync recipe as a plain MCP client."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("privacyfence-sync")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"
