# Baostock → QLib: HS300 Stock-Selection & Backtest Test Project

A self-contained pipeline that downloads **CSI300 (HS300)** daily data from
[baostock](https://baostock.com), converts it to the **QLib** `.bin` format, then trains a
**LightGBM / Alpha158** model, runs a **TopkDropoutStrategy** backtest against the
**SH000300** benchmark, and exports **stock selection + metrics + interactive charts** and a
**reusable local data cache**.

```
baostock  ──download──►  data/raw/*.csv  ──normalize──►  data/normalized/*.csv
          ──dump_bin──►  data/qlib_bin/{calendars,instruments,features}
          ──qlib.init──►  Alpha158 + LightGBM  ──►  TopkDropoutStrategy backtest
          ──►  output/{selected_stocks_latest.csv, pred.csv, metrics.json, *.html}
```

> QLib targets Python 3.8–3.12 and does **not** import on very new interpreters (e.g. 3.14 /
> pandas 3.0). All execution therefore happens in a dedicated conda env (`baostock_qlib`,
> Python 3.11) with `pyqlib` installed from a **prebuilt wheel** (no C compiler needed).

---

## 1. Prerequisites

- **conda** (Miniconda/Anaconda) on `PATH`.
- Internet access (for `conda`/`pip` and for baostock data).
- The QLib repository (this project lives at `<QLib>/baostock/` and reuses
  `<QLib>/scripts/dump_bin.py` + `<QLib>/scripts/data_collector/`).

## 2. Environment setup (one time)

```powershell
# from the QLib repo root
powershell -ExecutionPolicy Bypass -File baostock\setup_env.ps1
```

This creates `baostock_qlib` (Python 3.11), installs `pyqlib` + dependencies, and runs an
import smoke test. Verify manually at any time:

```powershell
conda run -n baostock_qlib python baostock\smoke_env.py     # prints "SMOKE OK"
```

Expected versions validated for this project: `pyqlib 0.9.7`, `numpy 2.x`, `pandas 2.3.x`,
`lightgbm 4.x`, `baostock 0.9.3`, `plotly 5.x (<6)`. The prebuilt `pyqlib` wheel ships the compiled
`qlib.data._libs.rolling` / `expanding` Cython ops, so **no MSVC build tools are required**.
`plotly` is pinned `<6` because `qlib.contrib.report.graph` imports `create_distplot`, which plotly
6.0 removed (see Troubleshooting).

## 3. Usage

Always invoke the entry scripts **by path** (`python baostock\<script>.py`). Running
`python -c "import qlib"` from the repo root would import the local `qlib/` *source* instead of
the installed wheel and fail with `ModuleNotFoundError: setuptools_scm` (see Troubleshooting).

### One-shot (download → dump → train → backtest → charts)

```powershell
# full run: HS300, 2014-01-01 .. 2024-12-31
conda run -n baostock_qlib python baostock\run_all.py

# fast smoke run: 20 symbols, 2018..2020, topk=10 (validates the whole flow in minutes)
conda run -n baostock_qlib python baostock\run_all.py --fast
```

### Step by step

```powershell
# Step 1 — download + normalize + dump to QLib .bin
conda run -n baostock_qlib python baostock\run_download.py
#   useful flags:
#     --start 2014-01-01 --end 2024-12-31   date window
#     --limit-nums 5                        only the first N symbols (debug)
#     --skip-download / --skip-dump         run just one half
#     --max-workers 4                       dump parallelism
#     --redownload                          ignore the raw cache and re-pull from baostock

# Step 2 — train / predict / backtest + selection / metrics / charts
conda run -n baostock_qlib python baostock\run_workflow.py
#   useful flags:
#     --topk 50 --n-drop 5                  strategy params
#     --market csi300 --benchmark SH000300
#     --train 2014-01-01,2019-12-31 --valid ... --test ...   override segments
#     --with-charts=False                   skip HTML chart export (see the fire note in section 8)

# Step 2b — only re-print / re-export the selection table (code + name) from cached pred.csv.
# No training, no qlib.init, no baostock call: seconds instead of the full run.
conda run -n baostock_qlib python baostock\run_workflow.py show
#   useful flags:  --topk 50   --pred_csv path\to\pred.csv   --out_dir path\to\output
```

### Declarative alternative (`qrun`)

`workflow/lgbm_alpha158.yaml` is the YAML equivalent of `run_workflow.py`. It is provided for
reference; note that `qrun` does **not** emit `selected_stocks_latest.csv`, `metrics.json` or the
HTML charts.

```powershell
conda run -n baostock_qlib qrun baostock\workflow\lgbm_alpha158.yaml
```

## 4. Outputs

| Path | Contents |
| --- | --- |
| `data/raw/<SYMBOL>.csv` | baostock raw (unadjusted) bars + post-adjusted close + derived `factor`; `_hs300_membership.csv` holds quarter-end constituent snapshots |
| `data/normalized/<SYMBOL>.csv` | QLib-schema adjusted `open/high/low/close/volume/amount/vwap/factor/change` |
| `data/qlib_bin/` | QLib dataset (`calendars/day.txt`, `instruments/{all,csi300}.txt`, `features/<symbol>/*.bin`) — the `provider_uri` |
| `output/selected_stocks_latest.csv` | top-K stocks by model score on the latest test date (`date,rank,instrument,name,score`); written as UTF-8-with-BOM so Excel shows the Chinese names |
| `output/pred.csv` | full prediction (score) matrix over the test segment — the cache `run_workflow.py show` re-reads |
| `output/metrics.json` | IC / ICIR / Rank-IC + portfolio risk analysis (annualized excess return w/ & w/o cost, information ratio, max drawdown) |
| `output/model_performance_*.html` | interactive plotly: cumulative group return, IC curve/heatmap/hist, score auto-correlation |
| `output/report_*.html` | interactive plotly: cumulative return vs benchmark, drawdown |
| `mlruns/` | MLflow experiment tracking (created by QLib `R.start`) |

`data/`, `output/` and `mlruns/` are git-ignored (large / regenerable). Open the `.html` charts
in any browser (they are self-contained).

Stock **names** are resolved from the already-downloaded `data/raw/_hs300_membership.csv` (baostock
returns `code_name` with each HS300 constituent snapshot), so naming the selection costs **no extra
baostock API call**. A symbol seen in several snapshots keeps its newest name; a symbol missing from
the cache gets an empty `name` rather than failing the export.

## 5. Expected runtime (rough)

| Stage | HS300 full (2014–2024) | `--fast` (20 symbols, 2018–2020) |
| --- | --- | --- |
| Download (baostock, sequential) | ~15–30 min | ~1–2 min |
| Normalize + dump `.bin` | ~1–3 min | ~10–30 s |
| **Alpha158 data load** (step 2) | **~30 min – 2 h+** | ~3–6 min |
| Train + backtest + charts | ~1–5 min | ~10–30 s |

baostock serves one global login session, so downloads are **sequential** (`max_workers=1`) with a
small inter-request delay. In step 2 the dominant cost is **not** training but the Alpha158 handler
materialising 158 features over the whole `(instruments × days)` grid; qlib 0.9.7 keeps no on-disk
feature cache, so this scales with both the universe and the window and the full 2014–2024 HS300
load can be slow on Windows. Use `--fast` to validate the flow in minutes, or narrow the window
(e.g. `--handler_start 2019-01-01 --fit_start 2019-01-01 --train 2019-01-01,2021-12-31
--valid 2022-01-01,2022-12-31 --test 2023-01-01,2024-12-31 --backtest_start 2023-01-01`) to cut it.

## 6. Data adjustment methodology

QLib stores **adjusted** prices with the convention `original_price = $close / $factor`. Rather
than interpreting baostock's adjust-factor event semantics, the collector derives the factor
**empirically** per date:

```
factor = close(adjustflag="1", post-adjusted) / close(adjustflag="3", raw)     # >= 1
open/high/low/close = raw * factor        vwap = (amount / volume) * factor
volume = raw_volume / factor              amount = raw amount (unchanged)
change = close.pct_change()
```

This keeps `price * volume == amount` and produces continuous post-adjusted series across
dividends/splits. The CSI300 **index** is stored unadjusted (`factor = 1`) as `SH000300`.

## 7. Caveats

- **Index membership is approximate.** Constituents are sampled at **quarter-ends**; each symbol
  gets a single `[first_seen, last_seen]` span in `instruments/csi300.txt`. This ignores
  intra-quarter add/remove events and introduces mild survivorship / look-ahead bias. It is
  sufficient for a test project but not for production-grade backtests.
- The last ~2 test dates have `NaN` labels (the label is `Ref($close,-2)/Ref($close,-1)-1`), which
  is expected; selection ranks by **score**, not label.
- **Names are snapshot-based.** `name` comes from the newest quarter-end membership row for that
  symbol, so a stock renamed after it left the index keeps its older name.
- **`show --topk N` rewrites the CSV.** Like `run --topk N`, it re-exports
  `selected_stocks_latest.csv` with N rows (the table is logged either way); re-run plain `show` to
  restore the configured `TOPK`.

## 8. Troubleshooting

- **`ModuleNotFoundError: setuptools_scm` / importing `F:\...\QLib\qlib\__init__.py`** — you ran
  `python -c "import qlib"` (or a script) from the **repo root**, so the local source shadowed the
  installed wheel. Fix: run the entry scripts **by path** (`python baostock\run_workflow.py`),
  which puts `baostock/` (not the repo root) on `sys.path[0]`. `smoke_env.py` prints the
  resolved `qlib.__file__` to confirm the wheel (`...\site-packages\qlib\...`) is used.
- **`AttributeError: module 'baostock' has no attribute ...`, or baostock calls silently returning
  nothing** — this project folder is *itself* named `baostock` and sits at the repo root. That is
  safe **only** because it has no `__init__.py`: Python prefers a regular package
  (`site-packages/baostock`) over a bare namespace directory at *any* `sys.path` position. Adding an
  `__init__.py` here would make it shadow the real library exactly like `qlib/` does.
  `smoke_env.py` prints the resolved `baostock.__file__` and warns if this folder ever wins.
- **`conda run` dies with `UnicodeEncodeError: 'gbk' codec can't encode ...` even though the child
  script succeeded** — `conda run` captures child stdout and re-prints it through the console code
  page. Either add `--no-capture-output`, or call the env's interpreter directly
  (`& $env:USERPROFILE\.conda\envs\baostock_qlib\python.exe baostock\<script>.py`). Note that
  re-redirecting an already-mangled log a second time (`*> file.log`, then piping it back) produces
  the same error with `\ufffd` as the offending character — read such a log from Python with
  `encoding="utf-16"`, not with `Get-Content`.
- **`ERROR: Could not consume arg: --no-with_charts` (or any `--no-<flag>`)** — these CLIs are built
  with `python-fire`, which does **not** synthesise a `--no-` negation for a parameter that defaults
  to `True`. Turn it off by value instead: `--with-charts=False`, `--with-calendar=False`,
  `--with-index=False`. Flags whose default is `False` *are* plain switches (`--full-refresh`,
  `--skip-basic`, `--quiet`). Hyphens and underscores are interchangeable throughout
  (`--skip-index-history` ≡ `--skip_index_history`), but only the value form is accepted for
  negation. `run_db.py <cmd> -- --help` prints the authoritative flag list.

  This one is **dangerous, not merely annoying**: fire calls the function with the *default* value
  first and only then fails on the leftover argument. So `run_db.py load-local --no-with-calendar`
  runs the **entire** load and exits 2 afterwards — it does not reject the flag up front. Always
  check the log body, not just the exit code.
- **`Gym has been unmaintained ... does not support NumPy 2.0`** — a harmless deprecation warning
  from `gym` (a transitive `pyqlib` dep used only for RL, which this project does not use). It does
  not affect LightGBM / Alpha158 / backtest.
- **`ImportError: cannot import name 'create_distplot' from 'plotly.figure_factory'`** — you have
  `plotly>=6`, but `qlib.contrib.report.graph` still imports `create_distplot` (removed in plotly
  6.0). Fix: `pip install "plotly<6"` (already pinned in `requirements.txt`). `_export_charts`
  degrades gracefully — it logs a warning and skips charts rather than aborting — so selection and
  `metrics.json` are still produced even if the chart import fails.
- **`UnicodeEncodeError: 'gbk' codec can't encode character ...`** — the console code page is GBK
  while the selection table now contains Chinese names. `run_workflow.py` re-points `sys.stdout` /
  `sys.stderr` at UTF-8 (`errors="replace"`) at import, which also fixes the `conda run` relay (it
  decodes child output as UTF-8). For your own scripts set `$env:PYTHONIOENCODING="utf-8"` first.
  Reading the CSV back with `Get-Content` still shows mojibake — it is UTF-8-with-BOM, so open it in
  Excel or `pd.read_csv(..., encoding="utf-8-sig")`.
- **baostock returns empty / `error_code != 0`** — transient session or rate-limit issues; the
  collector re-logs-in and retries. If it persists, increase `--delay` (e.g. `0.3`) and re-run
  (downloads are cached per symbol under `data/raw/`).
- **Dump is slow / hangs on Windows** — `dump_bin` uses `ProcessPoolExecutor` (spawn). Lower
  `--max-workers` (e.g. `1`) if constrained; entry modules are import-guarded so spawn is safe.
- **`csi300.txt` empty or backtest has no trades** — you downloaded fewer symbols than the
  membership list (e.g. `--limit-nums`). `csi300.txt` is intersected with the dumped features; use
  `--fast` (which also shrinks `topk`) or download the full universe.
- **Re-running / caching** — downloads are **cached per symbol**: `_simple_collector` skips any
  symbol whose `data/raw/<SYMBOL>.csv` already exists, so re-runs and resumes make **no extra
  baostock calls** for cached symbols (this also avoids `save_instrument` concatenating duplicate
  rows). The cache is window-agnostic — to re-pull a *different / wider* date range pass
  `--redownload` (or delete `data/raw/`). Delete `data/normalized/` + `data/qlib_bin/` to force a
  clean re-dump, and `mlruns/` to reset experiment tracking.

## 9. TimescaleDB sink (`astock`)

Sections 1–8 produce QLib `.bin` files. This section additionally lands the **same data in
PostgreSQL/TimescaleDB**, so it can be queried with plain SQL, joined against board/industry
metadata and inspected without going through QLib's expression engine. It is optional and
independent — the `.bin` workflow above keeps working whether or not the database exists.

### 9.1 Prerequisites

- A reachable PostgreSQL with the **TimescaleDB** extension (developed against PostgreSQL 18.6 +
  TimescaleDB 2.29.2 with `timescaledb.license = timescale`).
- `psycopg[binary]` — already pinned in `requirements.txt`.
- Connection defaults are `localhost:5432`, `postgres/postgres`, database **`astock`**. Each one can
  be overridden with the standard libpq variable, no code edit needed:

```powershell
$env:PGHOST="localhost"; $env:PGPORT="5432"; $env:PGUSER="postgres"
$env:PGPASSWORD="postgres"; $env:PGDATABASE="astock"
```

### 9.2 Usage

```powershell
# from the QLib repo root
conda run -n baostock_qlib python baostock\run_db.py init         # create db + extension + apply db/schema.sql
conda run -n baostock_qlib python baostock\run_db.py load-local   # stage 1: bars from the local CSV cache
conda run -n baostock_qlib python baostock\run_db.py sync-sector  # stage 2: boards/metadata from baostock
conda run -n baostock_qlib python baostock\run_db.py verify       # 49 checks against the CSV cache
conda run -n baostock_qlib python baostock\run_db.py all          # init + stage 1 + stage 2 + verify
```

- **Stage 1 (`load-local`) makes zero baostock calls.** It re-reads `data/raw/*.csv` — the cache the
  section-3 collector already downloaded — so re-running it is free. Useful flags:
  `--full-refresh` (ignore watermarks, reload everything), `--limit N` (first N symbols, debug),
  `--batch-symbols 50`, `--with-calendar=False`, `--with-index=False`.
- **Stage 2 (`sync-sector`) caches every baostock answer under `data/sector/`.** With a complete
  cache it logs in **zero** times and finishes in ~2 s. Useful flags: `--only industry`,
  `--skip-basic`, `--skip-index-history` (newest membership snapshot instead of all quarter-ends),
  `--skip-probe`, `--limit N`, `--delay 0.15`.
- Both stages are **idempotent**: `UNLOGGED` staging table → `COPY` → `DISTINCT ON` (last row wins
  when one file repeats a key) → `INSERT ... ON CONFLICT DO UPDATE`. A re-run never duplicates a bar.
- Compression is handled transparently — the loaders drop the compression policy and decompress the
  chunks they are about to touch, then restore both afterwards.
- `verify` exits **1** when any error-level check fails, so it is usable in CI; warnings do not.
- Two further subcommands, **`record-selection`** and **`refresh-returns`**, store the daily stock
  selection and its realized returns. They are documented in **section 10**.

### 9.3 Schema

| Object | Kind | Contents |
|---|---|---|
| `instrument` | table | one row per symbol: `code`, Chinese `code_name`, exchange, board, `ipo_date`/`out_date`, `is_index`, `hs300_first`/`hs300_last`, **`is_csi300_now`** |
| `trade_calendar` | table | trading days |
| `daily_bar` | **hypertable** | OHLCV plus `vwap`, `factor`, `pct_chg`, `turn`, `trade_status`, `is_st`; PK `(symbol, trade_date)`, 1-year chunks |
| `index_daily_bar` | **hypertable** | the CSI300 index itself (`SH000300`), stored **unadjusted** |
| `stock_board` | table | board membership; PK `(board_type, board_code, symbol, snapshot_date)` |
| `sync_log`, `sync_watermark` | tables | per-run bookkeeping, per-symbol high-water marks |
| `*_stg` | UNLOGGED tables | the `COPY` landing zone, truncated on each load |
| `daily_bar_weekly`, `daily_bar_monthly` | **continuous aggregates** | `time_bucket` OHLCV, kept current by policy |
| `v_instrument_csi300_now`, `v_daily_bar_csi300`, `v_daily_bar_adj_csi300` | views | **default** surface — current members only |
| `v_instrument_all`, `v_daily_bar_all`, `v_daily_bar_adj_all`, `v_daily_bar_trading` | views | **full** surface — all 700 union symbols, opt-in |
| `v_industry_latest`, `v_board_latest`, `v_index_membership_latest` | views | newest snapshot per symbol / board |
| `selection_run`, `selection_pick` + 2 views | tables / views | the daily top-K picks and their realized returns — **section 10** |

Both hypertables use `compress_segmentby = 'symbol'` and `compress_orderby = 'trade_date DESC'` with
a 90-day `add_compression_policy`. The continuous aggregates are created `WITH NO DATA`, back-filled
via `refresh_continuous_aggregate(...)` and then maintained by `add_continuous_aggregate_policy(...)`.

### 9.4 The 300-vs-700 scope rule

`daily_bar` deliberately holds **all 700 symbols that were ever in CSI300** during the window, not
just today's 300 — restricting it to current members would bake survivorship bias into every
historical backtest. What narrows to 300 is the **query surface**, via `instrument.is_csi300_now`:

- The default `v_*_csi300*` views expose only the 300 current members. `verify` fails if any of the
  400 non-current symbols leaks into them.
- The `v_*_all` views expose all 700 and must be asked for by name.

Measured split: `instrument` holds 701 rows (700 stocks + the `SH000300` index), of which
`is_csi300_now` = **300**, formally delisted (`out_date IS NOT NULL`) = **26**, and no longer in the
index for any reason = **400**.

### 9.5 Measured state

After `init` + `load-local` + `sync-sector` + `verify` on this machine:

| | |
|---|---|
| `daily_bar` | **1,921,545** rows, 700 symbols, 2014-01-02 … 2026-09-03 |
| `index_daily_bar` | 3,082 rows |
| `instrument` / `trade_calendar` | 701 / 4,629 rows |
| `stock_board` | **58,444** rows across 8 `board_type`s |
| `daily_bar_weekly` / `daily_bar_monthly` | 405,243 / 95,564 rows |
| on disk | `daily_bar` 129 MB in 14 chunks (**14 compressed**); `index_daily_bar` 1632 kB |
| `verify` | **49 checks, 0 failed, 1 warning** |

`stock_board` by type — 51 quarter-end snapshots spanning 2014-03-31 … 2026-09-03 unless noted:

| `board_type` | rows | symbols | snapshots |
|---|---|---|---|
| `index_zz500` | 25,499 | 1,413 | 51 |
| `index_hs300` | 15,300 | 700 | 51 |
| `terminated` | 8,028 | 334 | 51 |
| `starst` | 3,695 | 667 | 51 |
| `index_sz50` | 2,550 | 136 | 51 |
| `st` | 2,504 | 414 | 51 |
| `industry` | 674 | 674 | 1 (2026-08-31) |
| `suspended` | 194 | 45 | 36 (last 2022-12-30) |

Compression was validated with a decompress → re-compress round trip on the oldest chunk: 45,839 rows
with `count(*)`, `sum(volume)` and `min/max(close)` identical before and after, **8472 kB → 3688 kB
(43.5 % of the original)**, and the weekly continuous aggregate still matching a manual
`time_bucket` over the same rows (0 mismatched weeks). Measure chunk sizes with
`chunk_compression_stats('daily_bar')` — `pg_total_relation_size()` on a compressed chunk reports
only the evacuated original relation, which reads as a bogus ~99 % saving.

The one-query smoke test — current members with Chinese name, industry, latest close and
back-adjusted close — returns exactly 300 rows:

```sql
SELECT symbol, code_name, board, industry, last_trade_date, last_close, last_close_adj
FROM v_instrument_csi300_now
ORDER BY symbol;
```

```
SH600000  浦发银行  主板  J66货币金融服务  2026-09-03  close=9.27   adj=123.91
SH600009  上海机场  主板  G56航空运输业    2026-09-03  close=23.65  adj=63.42
SH600010  包钢股份  主板  C31黑色金属冶炼和压延加工业  2026-09-03  close=2.22   adj=16.08
```

41 distinct industries are present, across the 主板 / 创业板 / 科创板 boards, and 295 of the 300
symbols have `last_close_adj != last_close` — i.e. the back-adjust factor really is applied.

### 9.6 Known limitations

- **26 delisted symbols have no industry.** baostock answers with an *empty* `industry` for
  long-withdrawn members (measured: `sh.600005` 武钢股份, delisted 2017), so `stock_board` covers
  674 of the 700 union symbols. That is a data-source limit rather than a pipeline defect:
  `verify` **warns** on union-wide coverage but **fails** only if a *current* member lacks an
  industry — all 300 are covered.
- **`suspended` history stops at 2022-12-30**; the server returns nothing newer.
- **`ame` and `szhk` boards are unavailable** — the server rejects both with `10004020`. The
  collector probes each capability once, caches the verdict under `data/sector/` and never retries a
  rejected endpoint.
- **Industry classification is CSRC (证监会行业)**, e.g. `J66货币金融服务`, not GICS or 申万. It is a
  single current snapshot with no history, so it cannot be used point-in-time.
- **Financial statements are out of scope** at this stage — only bars, calendar, instruments and
  boards are loaded.
- **Membership is quarter-end sampled**, the same approximation as `instruments/csi300.txt` noted in
  section 7, so `stock_board` cannot resolve intra-quarter index changes.
- **Do not add an `__init__.py` to this folder** — see section 8.

## 10. Selection results in the database

Sections 1–3 answer “which stocks does the model pick today?”; this section makes the answer
**queryable by date**. One `selection_run` row per (signal date, strategy) carries the config
fingerprint and the run's IC/return metrics, and one `selection_pick` row per chosen stock carries
its rank, code, Chinese name, score, industry and — once known — its realized T+1/T+5/T+20 return
and excess over `SH000300`. Everything here is derived from files already on disk and bars already
in the database: **zero baostock calls, zero model calls.**

### 10.1 Usage

```powershell
# from the QLib repo root
conda run -n baostock_qlib python baostock\run_db.py record-selection               # latest signal date only
conda run -n baostock_qlib python baostock\run_db.py record-selection --backfill     # every date in output/pred.csv
conda run -n baostock_qlib python baostock\run_db.py refresh-returns                 # fill realized T+1/T+5/T+20
conda run -n baostock_qlib python baostock\run_db.py all --with-returns              # …and recompute before verify
```

`run_workflow.py` and `run_workflow.py show` record the selection **automatically** as their last
step, so a normal daily run needs no extra command. The write is *best-effort*: a database that is
down or not yet migrated only produces a `WARNING`, because the CSV and the log table are already
written by then and must never be lost to a storage problem. Turn it off with `--with-db=False`
(there is no `--no-with-db` — see the fire note in section 8).

Useful flags: `--pred-csv` / `--selection-csv` (read a different file), `--topk N` (re-rank the
backfill at a different K), `--with-db=False` (**dry run**: logs the date range, pick count and
`strategy_key` that *would* be written, without opening a connection), `--horizons 1,5`,
`--force` (recompute returns that are already filled, e.g. after a bar reload).

### 10.2 Schema

| Object | Kind | Contents |
|---|---|---|
| `selection_run` | table | one row per `(signal_date, strategy_key)`: experiment/recorder ids, `model_class`, `market`, `benchmark`, `topk`, `n_drop`, `segments` and `metrics` as `jsonb`, `n_picks`, `source`, `pred_csv`, `run_at`, `updated_at` |
| `selection_pick` | table | one row per chosen stock: `rank`, `symbol`, `code`, `code_name`, `score`, `industry`, `is_csi300_now`, `ret_t1/t5/t20`, `excess_t1/t5/t20`, `ret_computed_at`; PK `(signal_date, strategy_key, symbol)`, FK → `selection_run` `ON DELETE CASCADE` |
| `v_selection_latest` | view | one row per `(signal_date, symbol)`: when several strategies exist for a day it keeps the most recently updated one, and fills a missing name from `instrument` |
| `v_selection_performance` | view | per-day scoreboard: `n_picks`, `avg_score`, the three average returns and excesses, `n_scored_tN`, and `hit_rate_tN` |

Both are **plain tables, deliberately not hypertables**. The volume is ~12.5 k rows/year (50 picks ×
250 sessions), where chunking would cost more than it saves — and `refresh-returns` UPDATEs
already-stored rows repeatedly, which on a compressed hypertable would first mean dropping the
compression policy and decompressing the chunks. Two B-tree indexes serve the two access patterns:
`(signal_date DESC, rank)` for “the top-K table for one day”, `(symbol, signal_date DESC)` for
“every time this stock was picked”.

### 10.3 `signal_date` is not the run date

`signal_date` is the trading day the picks are **for** — the maximum date of the prediction matrix.
A job executed on the morning of D produces `signal_date = D-1`. Conflating the two shifts every
by-date query by a day, so the execution time is stored separately: `run_at` is when this
`(date, strategy)` was **first** recorded and is never overwritten, `updated_at` moves on every
re-record, and `selection_pick.loaded_at` does the same per row.

### 10.4 Re-running: overwrite vs. coexist

`strategy_key` is the first 16 hex of `sha1(canonical_json({experiment_name, model_class, market,
topk, n_drop, segments}))`. Only those six fields participate, so a new recorder id or a different
account size does **not** mint a new key and orphan the previous picks:

- **Same config again** → same key → the same rows are overwritten (`updated_at` refreshes, `run_at`
  stays). Verified: a full 20,300-row backfill re-run left both the pick and the run checksums
  byte-identical.
- **Different `topk` or model** → new key → the variants **coexist** and stay comparable side by
  side; `v_selection_latest` picks the newest per day.

Picks are written **delete-the-range, then `COPY`** inside one transaction rather than upserted,
because a top-K set can *shrink*: `--topk 50` followed by `--topk 10` would leave ranks 11–50 behind
as stale rows under a plain `ON CONFLICT DO UPDATE`. The audit trail is the existing `sync_log`
(`task = 'record_selection'` / `'refresh_returns'`), not a separate version table.

### 10.5 How the returns are computed

`refresh-returns` fills `ret_tN` and `excess_tN` with one set-based `UPDATE … FROM` per horizon:

- **Entry** is the back-adjusted close (`close * factor`) **on the signal date**, and requires
  `trade_status = 1` — a stock suspended that day could not actually have been bought, so it is left
  `NULL` rather than priced off a stale bar.
- **Exit** is the first tradable adjusted close **on or after** the T+N session, so a suspension at
  the exit rolls forward to the next session instead of producing a bogus price.
- **T+N counts trading days, not calendar days**, taken from `trade_calendar` via `row_number()`. All
  picks of one signal date therefore share a single exit day and stay cross-sectionally comparable.
- **Benchmark** legs come from `index_daily_bar` (`SH000300`), which is stored **unadjusted** — an
  index has no corporate actions to adjust for, so its factor is 1 by definition.
- Both values are `round(…, 6)`; `excess_tN = ret_tN − benchmark return` over the same window.

The command is **repeatable and cheap**: by default only rows still `NULL` are touched, so each
horizon is computed exactly once, on the first run after its exit day has bars. A non-zero
`still_null` for recent dates is expected, not an error.

`hit_rate_tN` in `v_selection_performance` divides by `count(excess_tN)`, **not** `count(*)`: rows
whose exit day has not happened yet are still `NULL` and must not drag the rate down.

### 10.6 Measured state

After `init` + `record-selection --backfill` + `refresh-returns` + `verify` on this machine:

| | |
|---|---|
| `selection_run` | **406** rows, one per signal date 2025-01-02 … 2026-09-03, all `strategy_key = d815bcd24bbf4963` |
| `selection_pick` | **20,300** rows (406 × 50); `code_name` filled for **20,300**, `industry` for **20,297** |
| `refresh-returns` | t1 **20,250** filled / 50 pending · t5 **20,050** / 250 · t20 **19,300** / 1,000 — exactly 1 / 5 / 20 sessions × 50 picks short of the 2026-09-03 bar frontier |
| second `refresh-returns` | `updated=0` on all three horizons (idempotent) |
| pooled hit rate | t1 **47.8 %** · t5 **48.7 %** · t20 **48.6 %** |
| average excess | t1 **+0.098 %** · t5 **+0.507 %** · t20 **+1.524 %** |
| `verify` | **49 checks, 0 failed, 1 warning** (the pre-existing 674/700 industry warning) |

The 13 new `selection` checks cover: object presence, `n_picks` vs. the actual child rows, every
`signal_date` being a real session, ranks being exactly `1..n` per group, every symbol existing in
`instrument`, `ret_computed_at` being set wherever a return is, no return priced beyond the bar
frontier, and — the strongest one — an **independent re-derivation** of `ret_tN`/`excess_tN` over a
deterministic sample of 10 signal dates. That cross-check uses `ORDER BY … OFFSET n` over
`trade_calendar` and prices off the stored `close_adj` column, whereas the write path uses
`row_number()` and `close * factor`, so agreement validates the calendar arithmetic and the
adjustment arithmetic separately instead of re-running one implementation against itself.

The two headline queries:

```sql
-- daily scoreboard
SELECT signal_date, n_picks, avg_excess_t5, hit_rate_t5, hit_rate_t20
FROM v_selection_performance ORDER BY signal_date DESC;

-- one day's full top-50, with Chinese names, industry and realized returns
SELECT "rank", symbol, code, code_name, score, industry,
       ret_t1, excess_t1, ret_t5, excess_t5, ret_t20, excess_t20
FROM v_selection_latest WHERE signal_date = DATE '2026-08-06' ORDER BY "rank";
```

### 10.7 Known limitations

- **The backfill only reaches as far back as `pred.csv` does** — the test segment, currently
  2025-01-01 onward. `pred.csv` holds predictions for that window only; recovering earlier history
  means re-running the workflow with an earlier `--test` segment, not re-running this command.
- **`ret_t20` is necessarily `NULL` for the last ~20 sessions**, `ret_t5` for the last ~5 and
  `ret_t1` for the final one: their exit days have no bars yet. This is the `still_null` count above
  and it shrinks by itself as the database is updated.
- **These returns are NOT the backtest's returns.** They are equal-weighted single-stock returns of
  “buy at the signal-date close, sell at the T+N close”, with no `n_drop` turnover, no transaction
  cost and no position sizing. `TopkDropoutStrategy`'s portfolio return in `metrics.json` includes
  all three, so the two numbers answer different questions and must not be compared directly.
- **A stock suspended on the signal date is never scored** for any horizon — there is no honest entry
  price. It stays `NULL` forever rather than being back-filled from a stale bar.
- **`industry` is the current CSRC snapshot** (section 9.6), so grouping historical picks by industry
  uses today's classification, not the one in force on the signal date.
- **Only T+1 / T+5 / T+20 exist.** They are fixed columns, not dynamic DDL; another horizon needs a
  schema change plus an entry in `HORIZONS`, which is also what keeps the f-string-built column names
  in the UPDATE safe from injection.
