---
kind: business_term
name: Business Glossary
category: business_term
scope:
    - '**'
---

### Provider
- Definition：Abstraction layer over data backends in Qlib. Concrete providers implement CalendarProvider, InstrumentProvider, FeatureProvider, PITProvider, ExpressionProvider, DatasetProvider, and a composite Provider; the default is LocalProvider backed by on-disk qlib binary files, but any custom provider can be plugged in via configuration.
- Aliases：data provider、LocalProvider、RemoteProvider

### DataHandler
- Definition：Interface that defines how raw market data is transformed into features and labels for training. Implementations live under qlib/contrib/data/handler and produce the Alpha158 / Alpha360 factor sets used by most benchmarks.
- Aliases：handler、DataHandlerLP

### DatasetH
- Definition：High-level dataset builder that wraps a DataHandler and splits the resulting feature/label tensors into train/valid/test segments; its `prepare()` method is the standard entry point consumed by Model.fit().
- Aliases：dataset handler wrapper

### Nested Decision Execution
- Definition：A Qlib pattern where multiple strategies/executors at different frequencies (e.g., daily portfolio allocation + intraday order execution) are composed and optimized together, driven by a single backtest loop.
- Aliases：nested executor、multi-frequency execution

### qrun
- Definition：CLI entry point (`python -m qlib.cli.run`) that loads a YAML workflow config and executes the full pipeline — dataset construction, model training, backtesting, and report generation — end to end.
- Aliases：workflow runner

### Alpha158 / Alpha360
- Definition：Two built-in factor/dataset configurations shipped with Qlib. Alpha158 provides 158 technical indicators per stock per day; Alpha360 extends the set to 360 factors. They are produced by DataHandler implementations and used as the standard benchmark datasets.
- Aliases：Alpha158 dataset、Alpha360 dataset

### Point-in-Time (PIT) data
- Definition：Financial data stored with a point-in-time semantics so that look-ahead bias is avoided during backtesting. Qlib exposes a dedicated PITProvider and PIT record schema (date/period/value/index).
- Aliases：PIT、point-in-time

### Region (REG_CN / REG_US / REG_TW)
- Definition：Market-region setting that controls trading conventions such as trade unit size, limit-up/limit-down thresholds, and deal price convention. Qlib ships presets for China A-shares, US equities, and Taiwan markets.
- Aliases：market region、region config

### Offline Mode / Online Mode
- Definition：Two deployment modes of Qlib's data server. Offline mode stores data locally on the machine running the process; Online mode deploys a shared data service (referenced as Qlib-Server on Azure) so multiple clients share one data copy and cache.
- Aliases：offline data server、online data server、qlib-server
