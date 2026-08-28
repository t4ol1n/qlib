---
kind: dependency_management
name: Python Dependency Management via pyproject.toml and Optional Extras
category: dependency_management
scope:
    - '**'
source_files:
    - pyproject.toml
    - setup.py
    - .github/workflows/test_qlib_from_source.yml
    - docs/requirements.txt
    - examples/benchmarks/LightGBM/requirements.txt
    - scripts/data_collector/yahoo/requirements.txt
---

## What system/approach is used

QLib manages dependencies exclusively through Python packaging tools. The single source of truth for the package's runtime and optional dependencies is `pyproject.toml`, which declares core dependencies, Python version constraints (`requires-python = ">=3.8.0"`), and a rich set of optional dependency groups (extras) installed via `pip install pyqlib[extra]`. Build-time dependencies are declared in `[build-system]` using `setuptools`, `setuptools-scm`, `cython`, and `numpy>=1.24.0`, with `setuptools.build_meta` as the build backend. A legacy `setup.py` remains only to compile two Cython C++ extensions (`qlib.data._libs.rolling` and `qlib.data._libs.expanding`) against NumPy headers.

There is no lockfile (no `requirements.txt` at the repo root, no `poetry.lock`, `Pipfile.lock`, or `uv.lock`). Version pinning is done inline inside `pyproject.toml` using PEP 508 specifiers (e.g. `pandas>=1.1`, `ruamel.yaml>=0.17.38`, `tianshou<=0.4.10`, `numpy<2.0.0`, `mypy<1.5.0`, `snowballstemmer<3.0`, `python-socketio<6`). Example scripts under `examples/` and `scripts/data_collector/` ship their own per-script `requirements.txt` files that use exact pins (e.g. `examples/benchmarks/LightGBM/requirements.txt` pins `pandas==1.1.2` and `numpy==1.21.0`; `scripts/data_collector/yahoo/requirements.txt` lists loose names). These are isolated from the main package and not consumed by pip when installing `pyqlib`.

## Key files and packages

- `pyproject.toml` — central manifest: project metadata, core `dependencies`, optional extras (`dev`, `rl`, `lint`, `docs`, `package`, `test`, `analysis`, `client`), entry point `qrun = qlib.cli.run:run`, setuptools-scm versioning config, and build-system requirements.
- `setup.py` — minimal shim declaring two Cython extensions built with C++ and NumPy include dirs; no dependency declarations here.
- `.github/workflows/test_qlib_from_source.yml` — CI installs PyTorch via platform-specific `pip install torch ... --extra-index-url https://download.pytorch.org/whl/cpu` on Ubuntu, then runs `make dev` (which uses the extras defined in `pyproject.toml`) and executes tests.
- `docs/requirements.txt` — separate list for building Sphinx docs locally (Cython, cmake, scipy, scikit-learn, pandas, tianshou, sphinx_rtd_theme); not consumed by the package installer.
- Per-example / per-script `requirements.txt` files under `examples/benchmarks/*/` and `scripts/data_collector/*/` — self-contained, tightly pinned environments for reproducing individual demos or collectors.

## Architecture and conventions

- **Single canonical manifest**: All package-level dependencies live in `pyproject.toml`; nothing is duplicated in `setup.py` or `setup.cfg`.
- **Optional features as extras**: Heavy or platform-specific libraries are split into extras so users can install only what they need:
  - `rl` adds reinforcement-learning stack (`tianshou<=0.4.10`, `torch`, `numpy<2.0.0`) — the numpy cap exists because PyTorch on macOS ≥3.10 cannot fully support NumPy ≥2.0.
  - `docs` pins `scipy<=1.15.3` and `snowballstemmer<3.0` to work around known build breakages in those upstream packages.
  - `client`, `analysis`, `test`, `lint`, `dev`, `package` group tooling and auxiliary features.
- **Version policy**: Core dependencies use minimum-version pins (`>=`) to allow patch updates while preventing breaking upgrades. Known incompatible upstream releases are explicitly capped (`<=` or `<`) with comments explaining why (e.g. MLflow `set_uri` artifact download bug, snowballstemmer 3.0 breaking docs builds, scipy 1.16.0 causing `_lazywhere` import errors).
- **No vendoring**: No `vendor/` directory, no vendored wheels, no private registry configuration. Dependencies resolve directly from PyPI (or the CPU-only PyTorch index in CI).
- **Build isolation**: Build-time deps (`cython`, `numpy>=1.24.0`) are separated from runtime deps via `[build-system]`; the runtime package itself does not require Cython at install time.
- **Per-script isolation**: Standalone data collectors and example notebooks ship their own `requirements.txt` files with exact pins, allowing reproducible demo environments independent of the main package.

## Conventions and constraints

- **Runtime Python version**: The package requires Python ≥3.8 and declares classifiers for 3.8–3.12.
- **Core dependencies** are declared once in `pyproject.toml` under `dependencies`; adding a new third-party library should go there rather than in `setup.py`.
- **Optional functionality must be expressed as an extra** in `[project.optional-dependencies]` so consumers can opt in (e.g. RL, docs, linting, client socket IO, analysis plotting).
- **Known-broken upstream versions are pinned with explanatory comments** in `pyproject.toml` — this is the documented way to handle upstream regressions.
- **CI installs PyTorch separately** from the extras matrix; it is not part of the default `pyqlib` install.
- **No lockfiles are committed**; reproducibility for examples relies on per-directory `requirements.txt` files with exact pins, while the main package relies on minimum-version ranges resolved by pip at install time.
- **Setuptools-scm drives versioning**: The package version is derived from git tags (`version_scheme = "guess-next-dev"`, `local_scheme = "no-local-version"`, written to `qlib/_version.py`); there is no static version string in the manifest.