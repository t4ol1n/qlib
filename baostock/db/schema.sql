-- Copyright (c) Microsoft Corporation.
-- Licensed under the MIT License.
--
-- Schema for the `astock` TimescaleDB sink (see baostock/db/init_db.py).
--
-- Idempotent by construction: tables/indexes use IF NOT EXISTS, hypertable conversion uses
-- if_not_exists => TRUE, views are DROPped then CREATEd (so a changed column list never trips
-- "cannot drop columns from view"), and policies are added with if_not_exists => TRUE.
--
-- Scope note: `daily_bar` holds the FULL 700-symbol HS300 union (historical constituents included,
-- so backtests are survivorship-bias free). The DEFAULT query surface is restricted to the 300
-- symbols currently in the index via `instrument.is_csi300_now`; the `*_all` views expose the union
-- explicitly.
--
-- `CREATE EXTENSION timescaledb` is issued by init_db.py before this file runs.

-- --------------------------------------------------------------------------- --
-- 1. Dimension tables
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS instrument (
    symbol          text        PRIMARY KEY,             -- QLib style: SH600000
    code            text        UNIQUE,                  -- baostock style: sh.600000
    code_name       text,                                -- Chinese name (UTF8 database)
    exchange        text,                                -- SH / SZ / BJ / INDEX
    board           text,                                -- 主板 / 创业板 / 科创板 / 北交所 / 指数
    sec_type        smallint,                            -- baostock query_stock_basic.type
    status          smallint,                            -- baostock query_stock_basic.status
    ipo_date        date,
    out_date        date,
    is_index        boolean     NOT NULL DEFAULT false,
    hs300_first     date,                                -- first quarter-end snapshot it appears in
    hs300_last      date,                                -- last  quarter-end snapshot it appears in
    is_csi300_now   boolean     NOT NULL DEFAULT false,  -- member of the LATEST snapshot (the switch
                                                         -- every default view filters on)
    updated_at      timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN instrument.is_csi300_now IS
    'True for the 300 symbols in the newest HS300 snapshot. Recomputed by each load; the default '
    'views (v_*_csi300*) filter on it so everyday queries only see the current index. For a '
    'point-in-time universe use stock_board with board_type=''index_hs300''.';

CREATE INDEX IF NOT EXISTS idx_instrument_csi300_now ON instrument (is_csi300_now) WHERE is_csi300_now;
CREATE INDEX IF NOT EXISTS idx_instrument_board      ON instrument (board);

CREATE TABLE IF NOT EXISTS trade_calendar (
    calendar_date   date     PRIMARY KEY,
    is_trading_day  smallint NOT NULL DEFAULT 1          -- baostock query_trade_dates semantics
);

-- --------------------------------------------------------------------------- --
-- 2. Daily bars (hypertables)
-- --------------------------------------------------------------------------- --
-- Prices are stored RAW (baostock adjustflag="3") plus the per-date adjustment `factor`
-- (post-adjusted close / raw close). Adjusted series are derived in v_daily_bar_adj_* following
-- the QLib convention used by collector/normalize_dump.py:
--     price_adj = price * factor,  volume_adj = volume / factor,  amount unchanged.
CREATE TABLE IF NOT EXISTS daily_bar (
    symbol          text           NOT NULL,
    trade_date      date           NOT NULL,
    open            numeric(18,6),
    high            numeric(18,6),
    low             numeric(18,6),
    close           numeric(18,6),
    preclose        numeric(18,6),
    volume          bigint,                              -- shares
    amount          numeric(20,4),                       -- CNY turnover
    vwap            numeric(18,6),                       -- raw amount/volume (NULL when volume=0)
    turn            numeric(12,6),                       -- turnover ratio, %
    pct_chg         numeric(12,6),                       -- baostock pctChg, %
    trade_status    smallint,                            -- 1 trading, 0 suspended
    is_st           smallint,                            -- 1 ST/*ST on that date
    factor          numeric(18,10),
    close_adj       numeric(18,6),                       -- baostock post-adjusted close
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable(
    'daily_bar', 'trade_date',
    chunk_time_interval => INTERVAL '1 year',
    if_not_exists       => TRUE
);
-- segmentby symbol: one compressed row-group per (symbol, chunk) -> single-symbol scans stay fast.
-- The PK (symbol, trade_date) contains the segmentby column, which compressed hypertables require.
ALTER TABLE daily_bar SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'trade_date DESC'
);
SELECT add_compression_policy('daily_bar', INTERVAL '90 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS index_daily_bar (
    symbol          text           NOT NULL,
    trade_date      date           NOT NULL,
    open            numeric(18,6),
    high            numeric(18,6),
    low             numeric(18,6),
    close           numeric(18,6),
    volume          bigint,
    amount          numeric(20,4),
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable(
    'index_daily_bar', 'trade_date',
    chunk_time_interval => INTERVAL '1 year',
    if_not_exists       => TRUE
);
ALTER TABLE index_daily_bar SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'trade_date DESC'
);
SELECT add_compression_policy('index_daily_bar', INTERVAL '90 days', if_not_exists => TRUE);

-- UNLOGGED staging targets for the COPY step: no WAL, no PK, TRUNCATEd after each flush.
-- Load path is COPY -> DISTINCT ON dedup -> INSERT ... ON CONFLICT DO UPDATE, so re-running the
-- loader never produces duplicate rows.
CREATE UNLOGGED TABLE IF NOT EXISTS instrument_stg (
    symbol          text,
    code            text,
    code_name       text,
    exchange        text,
    board           text,
    is_index        boolean,
    hs300_first     date,
    hs300_last      date,
    is_csi300_now   boolean
);
CREATE UNLOGGED TABLE IF NOT EXISTS trade_calendar_stg (
    calendar_date   date,
    is_trading_day  smallint
);
CREATE UNLOGGED TABLE IF NOT EXISTS stock_board_stg (
    board_type      text,
    board_code      text,
    board_class     text,
    symbol          text,
    code            text,
    code_name       text,
    snapshot_date   date
);
-- query_stock_basic() output, used to backfill instrument.ipo_date/out_date/sec_type/status.
CREATE UNLOGGED TABLE IF NOT EXISTS instrument_basic_stg (
    code            text,
    code_name       text,
    ipo_date        date,
    out_date        date,
    sec_type        smallint,
    status          smallint
);
CREATE UNLOGGED TABLE IF NOT EXISTS daily_bar_stg (
    symbol          text,
    trade_date      date,
    open            numeric(18,6),
    high            numeric(18,6),
    low             numeric(18,6),
    close           numeric(18,6),
    preclose        numeric(18,6),
    volume          bigint,
    amount          numeric(20,4),
    vwap            numeric(18,6),
    turn            numeric(12,6),
    pct_chg         numeric(12,6),
    trade_status    smallint,
    is_st           smallint,
    factor          numeric(18,10),
    close_adj       numeric(18,6)
);
CREATE UNLOGGED TABLE IF NOT EXISTS index_daily_bar_stg (
    symbol          text,
    trade_date      date,
    open            numeric(18,6),
    high            numeric(18,6),
    low             numeric(18,6),
    close           numeric(18,6),
    volume          bigint,
    amount          numeric(20,4)
);

-- --------------------------------------------------------------------------- --
-- 3. Boards / sectors (generic snapshot table)
-- --------------------------------------------------------------------------- --
-- One table for every board kind so a newly supported baostock endpoint needs ZERO DDL change:
--   board_type  'industry' | 'index_hs300' | 'index_sz50' | 'index_zz500' | 'st' | ...
--   board_code  the value within that type, e.g. 'J66货币金融服务' or 'st'
--   board_class classification system, e.g. '证监会行业分类'
CREATE TABLE IF NOT EXISTS stock_board (
    board_type      text        NOT NULL,
    board_code      text        NOT NULL,
    board_class     text,
    symbol          text        NOT NULL,                -- SH600000
    code            text,                                -- sh.600000
    code_name       text,
    snapshot_date   date        NOT NULL,
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (board_type, board_code, symbol, snapshot_date)
);
-- Serves "latest board of type T for symbol S" (v_industry_latest / v_board_latest).
CREATE INDEX IF NOT EXISTS idx_stock_board_symbol ON stock_board (symbol, board_type, snapshot_date DESC);
-- Serves "membership of index I at snapshot D" (v_index_membership_latest).
CREATE INDEX IF NOT EXISTS idx_stock_board_type   ON stock_board (board_type, snapshot_date DESC, board_code);

-- --------------------------------------------------------------------------- --
-- 4. Sync bookkeeping
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS sync_log (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task            text        NOT NULL,                -- load_local / collect_sector / ...
    source          text,                                -- local_csv / baostock
    params          jsonb,
    rows_fetched    integer,
    rows_written    integer,
    status          text,                                -- ok / failed / skipped
    error           text,
    started_at      timestamptz,
    finished_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sync_log_task ON sync_log (task, finished_at DESC);

CREATE TABLE IF NOT EXISTS sync_watermark (
    dataset         text        NOT NULL,                -- daily_bar / index_daily_bar
    symbol          text        NOT NULL,
    last_date       date,
    last_sync_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset, symbol)
);

-- --------------------------------------------------------------------------- --
-- 5. Views -- DEFAULT group (only the 300 symbols currently in CSI300)
-- --------------------------------------------------------------------------- --
DROP VIEW IF EXISTS v_instrument_csi300_now CASCADE;
CREATE VIEW v_instrument_csi300_now AS
SELECT
    i.symbol,
    i.code,
    i.code_name,
    i.exchange,
    i.board,
    i.ipo_date,
    i.out_date,
    i.hs300_first,
    i.hs300_last,
    ind.board_code            AS industry,
    ind.board_class           AS industry_classification,
    ind.snapshot_date         AS industry_date,
    last.trade_date           AS last_trade_date,
    last.close                AS last_close,
    last.close * last.factor  AS last_close_adj,
    last.pct_chg              AS last_pct_chg
FROM instrument i
LEFT JOIN LATERAL (
    SELECT sb.board_code, sb.board_class, sb.snapshot_date
    FROM stock_board sb
    WHERE sb.symbol = i.symbol AND sb.board_type = 'industry'
    ORDER BY sb.snapshot_date DESC
    LIMIT 1
) ind ON TRUE
LEFT JOIN LATERAL (
    SELECT b.trade_date, b.close, b.factor, b.pct_chg
    FROM daily_bar b
    WHERE b.symbol = i.symbol
    ORDER BY b.trade_date DESC
    LIMIT 1
) last ON TRUE
WHERE i.is_csi300_now;

DROP VIEW IF EXISTS v_daily_bar_csi300 CASCADE;
CREATE VIEW v_daily_bar_csi300 AS
SELECT b.*
FROM daily_bar b
JOIN instrument i ON i.symbol = b.symbol
WHERE i.is_csi300_now;

DROP VIEW IF EXISTS v_daily_bar_adj_csi300 CASCADE;
CREATE VIEW v_daily_bar_adj_csi300 AS
SELECT
    b.symbol,
    b.trade_date,
    i.code_name,
    b.open  * b.factor AS open,
    b.high  * b.factor AS high,
    b.low   * b.factor AS low,
    b.close * b.factor AS close,
    b.volume / NULLIF(b.factor, 0) AS volume,
    b.amount,
    b.vwap  * b.factor AS vwap,
    b.factor,
    b.pct_chg,
    b.turn,
    b.trade_status,
    b.is_st
FROM daily_bar b
JOIN instrument i ON i.symbol = b.symbol
WHERE i.is_csi300_now;

-- --------------------------------------------------------------------------- --
-- 6. Views -- FULL group (all 700 union symbols; use explicitly)
-- --------------------------------------------------------------------------- --
DROP VIEW IF EXISTS v_instrument_all CASCADE;
CREATE VIEW v_instrument_all AS
SELECT
    i.symbol,
    i.code,
    i.code_name,
    i.exchange,
    i.board,
    i.sec_type,
    i.status,
    i.ipo_date,
    i.out_date,
    i.is_index,
    i.hs300_first,
    i.hs300_last,
    i.is_csi300_now,
    ind.board_code            AS industry,
    ind.board_class           AS industry_classification,
    ind.snapshot_date         AS industry_date
FROM instrument i
LEFT JOIN LATERAL (
    SELECT sb.board_code, sb.board_class, sb.snapshot_date
    FROM stock_board sb
    WHERE sb.symbol = i.symbol AND sb.board_type = 'industry'
    ORDER BY sb.snapshot_date DESC
    LIMIT 1
) ind ON TRUE;

DROP VIEW IF EXISTS v_daily_bar_all CASCADE;
CREATE VIEW v_daily_bar_all AS
SELECT * FROM daily_bar;

DROP VIEW IF EXISTS v_daily_bar_adj_all CASCADE;
CREATE VIEW v_daily_bar_adj_all AS
SELECT
    b.symbol,
    b.trade_date,
    b.open  * b.factor AS open,
    b.high  * b.factor AS high,
    b.low   * b.factor AS low,
    b.close * b.factor AS close,
    b.volume / NULLIF(b.factor, 0) AS volume,
    b.amount,
    b.vwap  * b.factor AS vwap,
    b.factor,
    b.pct_chg,
    b.turn,
    b.trade_status,
    b.is_st
FROM daily_bar b;

-- Suspended days (tradestatus=0) are kept in daily_bar as-is; filter them here. The 33 rows that
-- data/normalized/*.csv drops relative to data/raw/*.csv are exactly these.
DROP VIEW IF EXISTS v_daily_bar_trading CASCADE;
CREATE VIEW v_daily_bar_trading AS
SELECT * FROM daily_bar WHERE trade_status = 1;

-- --------------------------------------------------------------------------- --
-- 7. Views -- boards
-- --------------------------------------------------------------------------- --
DROP VIEW IF EXISTS v_industry_latest CASCADE;
CREATE VIEW v_industry_latest AS
SELECT DISTINCT ON (symbol)
    symbol, code, code_name,
    board_code  AS industry,
    board_class AS industry_classification,
    snapshot_date
FROM stock_board
WHERE board_type = 'industry'
ORDER BY symbol, snapshot_date DESC;

DROP VIEW IF EXISTS v_board_latest CASCADE;
CREATE VIEW v_board_latest AS
SELECT DISTINCT ON (board_type, symbol)
    board_type, symbol, code, code_name,
    board_code, board_class, snapshot_date
FROM stock_board
ORDER BY board_type, symbol, snapshot_date DESC;

DROP VIEW IF EXISTS v_index_membership_latest CASCADE;
CREATE VIEW v_index_membership_latest AS
SELECT
    board_type, board_code AS index_code, snapshot_date, symbol, code, code_name
FROM stock_board b
WHERE b.board_type LIKE 'index\_%'
  AND b.snapshot_date = (
      SELECT max(b2.snapshot_date) FROM stock_board b2 WHERE b2.board_type = b.board_type
  )
ORDER BY board_type, symbol;

-- --------------------------------------------------------------------------- --
-- 8. Continuous aggregates (weekly / monthly bars)
-- --------------------------------------------------------------------------- --
-- Created WITH NO DATA: the historical backfill is an explicit
-- `CALL refresh_continuous_aggregate(<view>, NULL, NULL)` issued by load_local.py right after the
-- bars land (a policy with a bounded start_offset would never reach 2014).
CREATE MATERIALIZED VIEW IF NOT EXISTS daily_bar_weekly
WITH (timescaledb.continuous) AS
SELECT
    symbol,
    time_bucket(INTERVAL '1 week', trade_date) AS bucket,
    first(open,  trade_date)   AS open,
    max(high)                  AS high,
    min(low)                   AS low,
    last(close, trade_date)    AS close,
    sum(volume)::bigint        AS volume,
    sum(amount)                AS amount,
    avg(turn)                  AS turn,
    count(*)                   AS n_days
FROM daily_bar
GROUP BY symbol, time_bucket(INTERVAL '1 week', trade_date)
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS daily_bar_monthly
WITH (timescaledb.continuous) AS
SELECT
    symbol,
    time_bucket(INTERVAL '1 month', trade_date) AS bucket,
    first(open,  trade_date)   AS open,
    max(high)                  AS high,
    min(low)                   AS low,
    last(close, trade_date)    AS close,
    sum(volume)::bigint        AS volume,
    sum(amount)                AS amount,
    avg(turn)                  AS turn,
    count(*)                   AS n_days
FROM daily_bar
GROUP BY symbol, time_bucket(INTERVAL '1 month', trade_date)
WITH NO DATA;

SELECT add_continuous_aggregate_policy('daily_bar_weekly',
    start_offset     => INTERVAL '6 months',
    end_offset       => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists    => TRUE);
SELECT add_continuous_aggregate_policy('daily_bar_monthly',
    start_offset     => INTERVAL '2 years',
    end_offset       => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists    => TRUE);
