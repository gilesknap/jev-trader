"""Top level API.

.. data:: __version__
    :type: str

    Version number as calculated by https://github.com/pypa/setuptools_scm
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("trader")
except PackageNotFoundError:  # run from a source tree that isn't installed (e.g. PYTHONPATH=src)
    __version__ = "0+unknown"

__all__ = ["__version__"]
