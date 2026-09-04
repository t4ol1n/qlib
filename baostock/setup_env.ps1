# Setup the isolated conda environment for the baostock -> QLib test project.
# QLib supports Python 3.8-3.12; the base interpreter here (3.14) is incompatible, so we
# create a dedicated env and install pyqlib from a prebuilt wheel (no C compiler needed).
#
# Usage (from the QLib repo root):
#   powershell -ExecutionPolicy Bypass -File baostock\setup_env.ps1

$ErrorActionPreference = "Stop"
$EnvName = "baostock_qlib"
$PyVer = "3.11"
$ReqFile = "baostock/requirements.txt"

Write-Host "=== [1/4] conda create -n $EnvName python=$PyVer ===" -ForegroundColor Cyan
conda create -y -n $EnvName "python=$PyVer"

Write-Host "=== [2/4] upgrade pip ===" -ForegroundColor Cyan
conda run --no-capture-output -n $EnvName python -m pip install --upgrade pip setuptools wheel

# Install everything from requirements.txt (pyqlib + extras). Doing it via a file avoids the
# PowerShell/cmd '<' redirection problem that breaks inline specs like "numpy<2.0".
Write-Host "=== [3/4] install pyqlib + dependencies from $ReqFile ===" -ForegroundColor Cyan
conda run --no-capture-output -n $EnvName pip install -r $ReqFile

# Smoke test as a SCRIPT (not `python -c`): running baostock/smoke_env.py puts
# baostock/ on sys.path[0], so `import qlib` resolves to the installed wheel instead of
# the local repo-root source (which would fail with ModuleNotFoundError: setuptools_scm).
# NOTE: this folder is named `baostock` and sits at the repo root, but it does NOT shadow the
# installed baostock package: it has no __init__.py, so Python sees only a namespace portion and a
# real package (site-packages/baostock/__init__.py) wins regardless of sys.path order.
# Do NOT add an __init__.py here -- that would make it a regular package and shadow the real one,
# exactly like the repo-root qlib/ source does. smoke_env.py asserts the installed one wins.
Write-Host "=== [4/4] smoke test imports ===" -ForegroundColor Cyan
conda run --no-capture-output -n $EnvName python baostock/smoke_env.py

Write-Host "=== setup_env done ===" -ForegroundColor Green
