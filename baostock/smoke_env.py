# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Environment smoke test for the isolated ``baostock_qlib`` conda env.

Run this from anywhere as a *script* (never ``python -c`` from the repo root)::

    conda run -n baostock_qlib python baostock/smoke_env.py

Executing as a script puts ``baostock/`` (not the QLib repo root) on
``sys.path[0]``, so ``import qlib`` resolves to the installed ``pyqlib`` wheel
instead of the local source checkout (which needs ``setuptools_scm`` + a
compiler-built ``_libs``). The check below asserts exactly that.

``import baostock`` is checked for the same class of problem: this folder is named ``baostock``
and sits at the repo root. It is safe *only* because it has no ``__init__.py`` -- Python prefers a
regular package (``site-packages/baostock``) over a bare namespace directory at any ``sys.path``
position. Adding an ``__init__.py`` here would make it shadow the real package, so the check below
asserts the installed one still wins.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent  # .../QLib


def main() -> int:
    print(f"python       = {sys.version.split()[0]}  ({sys.executable})")
    print(f"sys.path[0]  = {sys.path[0]}")

    import numpy as np
    import pandas as pd

    print(f"numpy        = {np.__version__}")
    print(f"pandas       = {pd.__version__}")

    import qlib

    qlib_file = Path(qlib.__file__).resolve()
    print(f"qlib         = {getattr(qlib, '__version__', '?')}  <- {qlib_file}")
    try:
        # The wheel MUST be used, not the repo-root source checkout.
        qlib_file.relative_to(REPO_ROOT)
        is_repo_source = True
    except ValueError:
        is_repo_source = False
    if is_repo_source:
        print("WARNING: imported the LOCAL qlib source (repo root is on sys.path).")
        print("         Run scripts as `python baostock/<script>.py`, not `python -c`.")

    # Compiled Cython ops (only present in the prebuilt wheel or a source build).
    from qlib.data._libs import rolling, expanding  # noqa: F401

    print("qlib._libs   = rolling + expanding OK")

    import baostock  # noqa: F401
    import lightgbm as lgb
    import plotly
    import scipy
    import statsmodels  # noqa: F401
    import fire  # noqa: F401
    import loguru  # noqa: F401

    if getattr(baostock, "__version__", None):
        print(f"baostock     = {baostock.__version__}  <- {baostock.__file__}")
    else:
        # A namespace package built from THIS folder has neither __version__ nor __file__.
        print("WARNING: `import baostock` resolved to THIS project folder, not the installed")
        print("         package. This folder must NOT contain an __init__.py: without one Python")
        print("         prefers site-packages/baostock, with one this folder shadows it.")
    print(f"lightgbm     = {lgb.__version__}")
    print(f"plotly       = {plotly.__version__}")
    print(f"scipy        = {scipy.__version__}")

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
