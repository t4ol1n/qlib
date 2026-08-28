---
kind: configuration_system
name: 'QLib Configuration System: QSettings, QlibConfig, YAML Workflows, and Environment-Driven Settings'
category: configuration_system
scope:
    - '**'
source_files:
    - qlib/config.py
    - qlib/__init__.py
    - qlib/cli/run.py
    - qlib/workflow/exp.py
    - qlib/workflow/utils.py
    - examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml
    - examples/workflow_by_code.py
---

## Overview

QLib uses a layered configuration system that combines Pydantic-based environment-driven settings, a mutable global `QlibConfig` object, and declarative YAML workflow definitions. Configuration is loaded at process start via `qlib.init()` (or the CLI `qrun`) and then propagated to every subsystem through a single global `C` instance.

## Core Components

### 1. Environment-driven defaults — `QSettings` (`qlib/config.py`)

- Built on `pydantic_settings.BaseSettings` with `env_prefix="QLIB_"` and `env_nested_delimiter="_"`, so nested fields like `mlflow.uri` are read from `QLIB_MLFLOW_URI`. Defaults include `provider_uri="~/.qlib/qlib_data/cn_data"` and MLflow URI pointing to `<cwd>/mlruns`.
- A module-level singleton `QSETTINGS = QSettings()` is consumed by the default `_default_config` dict (e.g., `exp_manager.kwargs.uri` reads `QSETTINGS.mlflow.uri`).

### 2. Global mutable config — `QlibConfig` (`qlib/config.py`)

- `QlibConfig` extends a small `Config` base that wraps a Python dict with attribute-style access (`C.provider_uri`, `C["region"]`) and supports `reset()`, `update()`, and `set_conf_from_C()`.
- `_default_config` defines all built-in keys: provider classes (`LocalCalendarProvider`, `LocalFeatureProvider`, …), cache backends (`DiskDatasetCache`, `DiskExpressionCache`, `SimpleDatasetCache`), Redis connection params, logging config, experiment manager class path, PIT record dtypes/nan sentinels, MongoDB task URL, `min_data_shift`, etc.
- Two preset mode dicts — `MODE_CONF["client"]` and `MODE_CONF["server"]` — are applied via `C.set_mode(mode)`; they differ mainly in `provider_uri`, cache strategy, `mount_path`, `auto_mount`, `timeout`, `logging_level`, and `region`.
- Region presets in `_default_region_config` set `trade_unit`, `limit_threshold`, `deal_price` for `REG_CN`, `REG_US`, `REG_TW`.
- `C.set(default_conf="client", **kwargs)` is the canonical entry point: it resets state, applies logging config, calls `set_mode`, `set_region`, merges kwargs (with an unrecognized-key warning), resolves NFS/local paths via `DataPathManager`, and validates Redis-backed caches against `can_use_cache()`.
- `C.register()` wires up custom ops/wrappers, instantiates the experiment manager from `exp_manager` config, registers `QlibRecorder` into the global `R`, and installs an exit handler.

### 3. Process initialization — `qlib.init()` (`qlib/__init__.py`)

- Signature: `init(default_conf="client", clear_mem_cache=True, skip_if_reg=False, **kwargs)`.
- Clears memory cache (`H.clear()`), delegates to `C.set(...)`, sets global log level, then mounts NFS URIs when `provider_uri` looks like `host:/path` (via `_mount_nfs_uri`, which can auto-mount or raise actionable errors).
- Finally calls `C.register()` to activate the configured experiment backend.
- Convenience helpers: `init_from_yaml_conf(conf_path, ...)` loads a top-level YAML and calls `init`; `auto_init(**kwargs)` walks upward from the current file to find a project `config.yaml`, supporting two modes:
  - `conf_type: "origin"` — treat the file as a direct `qlib.init(...)` call.
  - `conf_type: "ref"` — load a shared `qlib_cfg` and merge local `qlib_cfg_update` overrides on top of it.

### 4. Workflow-level configuration — YAML files (`examples/benchmarks/*/workflow_config_*.yaml`)

- Each benchmark ships a `workflow_config_<model>_<alpha>.yaml` defining four top-level sections:
  - `qlib_init`: passed directly to `qlib.init(...)` (e.g., `provider_uri`, `region`).
  - `task.model`, `task.dataset`, `task.record`: class/module_path/kwargs triples resolved via `init_instance_by_config` during `task_train`.
  - `market`, `benchmark`, `data_handler_config`, `port_analysis_config`: reusable anchors (`&name` / `*name`) for DRY composition.
- The CLI entrypoint `qlib/cli/run.py` exposes `qrun <config_path>` via `fire.Fire(workflow)`. Its `workflow()` function:
  1. Renders the YAML through Jinja2, substituting any undeclared template variables from `os.environ`.
  2. Loads with `ruamel.yaml.YAML(typ="safe")`.
  3. Optionally loads a `BASE_CONFIG_PATH` base file and deep-merges it with the user config via `qlib.utils.data.update_config`.
  4. Applies the `sys` section to extend `sys.path` (`sys.path`, `rel_path`).
  5. Calls `qlib.init(**config.get("qlib_init"))`, overriding `exp_manager.kwargs.uri` to `<cwd>/<uri_folder>` if not provided.
  6. Runs `task_train(config["task"], experiment_name=...)` and saves the full config into the recorder.

### 5. Experiment/recorder configuration

- The default experiment manager is `MLflowExpManager` (`module_path: qlib.workflow.expm`, class: `MLflowExpManager`) instantiated from `C["exp_manager"]`.
- `qlib.workflow.exp.MLflowExperiment` wraps MLflow's tracking client; `recorder_status` constants (`STATUS_S`, `STATUS_FI`, `STATUS_FA`) control run lifecycle. An `atexit` hook plus `sys.excepthook` ensure experiments end in FAILED status on uncaught exceptions.
- Record templates under `qlib.workflow.record_temp` (`SignalRecord`, `SigAnaRecord`, `PortAnaRecord`) are referenced by name in YAML `task.record` lists.

## Architecture & Conventions

| Layer | Mechanism | Purpose |
|---|---|---|
| Defaults | `_default_config` dict + `MODE_CONF` / `_default_region_config` | Central source of truth for every subsystem key |
| Overrides | `QSettings` env vars (`QLIB_*`) | Environment-driven tuning without code changes |
| Runtime mutation | `C.set(...)` + `C.update(...)` | Single mutable global accessed everywhere |
| Workflow definition | YAML with `qlib_init` + `task.*` | Declarative pipelines for benchmarks and users |
| Template layering | Jinja2 rendering + `BASE_CONFIG_PATH` + `update_config` | Shareable base configs with per-run overrides |
| Path resolution | `QlibConfig.DataPathManager` | Normalizes `provider_uri` (local vs NFS) and maps to `mount_path` |
| Lifecycle hooks | `C.register()` + `experiment_exit_handler` | Ensures exp manager, ops, wrappers, and cleanup are wired once |

## Conventions & Constraints

- **Single global**: All components read configuration exclusively from the module-level `C` (a `QlibConfig`); there is no per-call config passing beyond `qlib.init(...)`.
- **Mode-first setup**: Users pick `client` or `server` via `default_conf` in `qlib.init`; region-specific values are then layered on top via `region=REG_CN|REG_US|REG_TW`.
- **NFS awareness**: `provider_uri` may be a local path or an NFS URI (`host:/path`). `C.resolve_path()` requires a matching `mount_path[freq]` for every frequency key; missing frequencies trigger an assertion.
- **Redis cache fallback**: If `expression_cache` or `dataset_cache` depends on Redis but `can_use_cache()` fails, those caches are silently disabled with a warning.
- **Unrecognized kwargs warn**: Any key passed to `C.set(...)` that is not in the current config emits a warning rather than raising — used to surface typos in YAML.
- **YAML anchors**: Benchmark configs use YAML anchors (`&market`, `*market`) to reuse instrument lists and analysis blocks across models.
- **Template variables**: Jinja2-rendered configs automatically pull undefined variables from `os.environ`, enabling secret/environment injection without modifying YAMLs.
- **Base config inheritance**: `BASE_CONFIG_PATH` lets a workflow inherit a shared baseline and override only changed fields via `update_config`.
- **Experiment output directory**: When `qlib_init.exp_manager.kwargs.uri` is absent, the CLI forces it to `file:<cwd>/<uri_folder>` (default `mlruns`).
- **Version pinning**: `C.reset_qlib_version()` allows a caller to override `qlib.__version__` via `qlib_reset_version` for backward-compatible server connections.