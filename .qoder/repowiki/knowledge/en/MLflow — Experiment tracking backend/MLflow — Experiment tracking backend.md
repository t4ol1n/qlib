---
kind: external_dependency
name: MLflow — Experiment tracking backend
slug: mlflow
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

- Qlib uses MLflow as the experiment manager (`MLflowExpManager` configured via `exp_manager` dict in `_default_config`).
- Default URI is a local filesystem directory `<cwd>/mlruns`; can be overridden through `QLIB_MLFLOW_URI` env var or `qlib.init`.
- The workflow layer (`QlibRecorder`, `Experiment`, `Recorder`) logs metrics, artifacts and model objects into this MLflow store.
- Verify exact API/params against official MLflow docs when changing the backend.