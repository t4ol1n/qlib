---
kind: error_handling
name: QLib Exception Hierarchy and Workflow Error Propagation
category: error_handling
scope:
    - '**'
source_files:
    - qlib/utils/exceptions.py
    - qlib/workflow/expm.py
    - qlib/workflow/recorder.py
    - qlib/workflow/__init__.py
    - qlib/workflow/record_temp.py
    - qlib/workflow/task/collect.py
    - qlib/workflow/online/utils.py
---

## Overview

QLib defines a small, focused exception hierarchy under `qlib/utils/exceptions.py` and propagates those exceptions through the workflow layer (experiment/recorder management). Most other modules raise standard Python built-in exceptions (`ValueError`, `NotImplementedError`, `RuntimeError`, `AssertionError`). There is no repository-wide middleware or panic/recover mechanism; error handling is localized to the boundaries where external subsystems (MLflow, file I/O, pickle) are accessed.

## Custom Exception Types

The entire custom exception surface lives in one file:

- `QlibException(Exception)` — base class for Qlib-specific errors.
- `RecorderInitializationError(QlibException)` — raised when a user tries to reinitialize Qlib while an experiment/recorder is already active.
- `LoadObjectError(QlibException)` — raised when loading a saved object (e.g. from MLflow artifacts) fails during unpickling/download.
- `ExpAlreadyExistError(Exception)` — raised when attempting to create an experiment that already exists (not derived from `QlibException`).

These types are intentionally narrow: they model domain-level failures of the experiment/recording subsystem rather than general validation errors.

## Where Exceptions Are Raised

| Location | Pattern | Details |
|---|---|---|
| `qlib/workflow/expm.py` (`MLflowExpManager.create_exp`) | Wraps `mlflow.exceptions.MlflowException` with code `RESOURCE_ALREADY_EXISTS` into `ExpAlreadyExistError` using `raise ... from e`. | Centralizes the translation from the MLflow backend error into a Qlib-level signal. |
| `qlib/workflow/__init__.py` (`RecorderWrapper.register`) | Raises `RecorderInitializationError` if `_provider.exp_manager.active_experiment` is not `None`. | Prevents silent state corruption caused by reinitializing Qlib mid-experiment. |
| `qlib/workflow/recorder.py` (`MLflowRecorder.load_object`) | Catches any `Exception` during artifact download + unpickle and raises `LoadObjectError(str(e)) from e`. | Normalizes arbitrary storage/unpickle failures into a single typed error for callers. |
| `qlib/workflow/record_temp.py` | Raises `QlibException` directly for invalid strategy configuration passed to `MultiPassPortAnaRecord`. | Uses the base `QlibException` for configuration validation failures inside the recorder pipeline. |
| `qlib/workflow/task/collect.py`, `qlib/workflow/online/utils.py` | Catch `LoadObjectError` and handle it (e.g. skip missing objects). | Demonstrates the intended consumer pattern: catch the specific `LoadObjectError` rather than a bare `Exception`. |

## Conventions Observed

1. **Wrap third-party exceptions at subsystem boundaries.** The only place Qlib translates another library's error type is in `expm.py`, where `MlflowException(RESOURCE_ALREADY_EXISTS)` is converted to `ExpAlreadyExistError`. Other layers generally let underlying exceptions bubble up.
2. **Use `from e` chaining** when re-raising. Both `ExpAlreadyExistError` and `LoadObjectError` preserve the original traceback via `raise X(...) from e`, which aids debugging.
3. **Prefer typed exceptions over generic ones for cross-module contracts.** Callers in `task/collect.py` and `online/utils.py` explicitly `except LoadObjectError`, showing that this type is treated as a stable contract between the recording layer and consumers.
4. **Configuration/validation errors use built-ins or the base `QlibException`.** Invalid arguments in `record_temp.py` raise `QlibException` directly; most other validation uses `ValueError` / `NotImplementedError` / `AssertionError` without a custom wrapper.
5. **No global try/except or middleware.** There is no central error-handling interceptor around workflows, strategies, or backtests. Errors propagate upward to the caller (often the CLI or example scripts), which decide whether to log and exit.
6. **No `panic`/`recover` equivalent.** Python conventions are followed throughout; there are no `try/finally` blocks used as recovery mechanisms beyond normal cleanup in `MLflowRecorder.end_run`.

## Key Files

- `qlib/utils/exceptions.py` — sole definition of custom exception types.
- `qlib/workflow/expm.py` — experiment creation and lookup; translates MLflow errors to `ExpAlreadyExistError`.
- `qlib/workflow/recorder.py` — `MLflowRecorder.load_object` wraps storage/pickle failures into `LoadObjectError`.
- `qlib/workflow/__init__.py` — `RecorderWrapper.register` enforces single-initialization via `RecorderInitializationError`.
- `qlib/workflow/record_temp.py` — raises `QlibException` for invalid multi-pass portfolio analysis inputs.
- `qlib/workflow/task/collect.py`, `qlib/workflow/online/utils.py` — consumers that catch `LoadObjectError`.

## Scope Limitation

This error-handling system is concentrated in the workflow/experiment-recording subsystem. Data loaders, models, strategies, backtest engines, RL trainers, and CLI entry points mostly rely on Python built-in exceptions and do not import or reference the custom exception types. Consequently, the custom hierarchy should be considered a workflow-layer convention rather than a repository-wide policy.