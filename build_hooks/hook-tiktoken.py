"""PyInstaller hook for tiktoken.

tiktoken uses pkgutil.iter_modules() to discover encoding plugins
at runtime from the tiktoken_ext namespace package. PyInstaller does
not automatically discover namespace packages scanned via iter_modules,
so we must explicitly collect them.
"""

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("tiktoken_ext")
