# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Stage 2 -- collect baostock board / sector data into ``stock_board``.

Two phases, deliberately split so a re-run costs zero API calls:

1. **collect** -- baostock -> ``data/sector/*.csv`` (append-only, resumable; a Ctrl-C keeps whatever
   was already fetched and the next run only asks for the missing codes / dates).
2. **load**    -- ``data/sector/*.csv`` -> ``stock_board`` (COPY -> staging -> upsert) and, from
   ``basic.csv``, backfill ``instrument.ipo_date/out_date/sec_type/status``.

Endpoint availability was measured, not assumed:

* per-code (safe): ``query_stock_industry``, ``query_stock_basic``
* per-date (safe): ``query_hs300_stocks`` (served from the local membership cache instead),
  ``query_sz50_stocks``, ``query_zz500_stocks``, ``query_st_stocks``
* per-date, probed once at run time and cached in ``capability.csv`` -- measured **supported**:
  ``query_starst_stocks``, ``query_terminated_stocks``, ``query_suspended_stocks``; measured
  **rejected** (10004020): ``query_ame_stocks``, ``query_szhk_stocks``
* **rejected by the server** (``error_code 10004020 错误的消息类型``) -> never called:
  ``query_stock_concept``, ``query_stock_area``, ``query_gem_stocks``, ``query_shhk_stocks``,
  ``query_stocks_in_risk``
* **never called with an empty ``code``**: the all-market metadata request spins forever (measured:
  >5 min of CPU with no reply), so every call is scoped to one code or one date.
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence

import pandas as pd
from loguru import logger
from tqdm import tqdm

CUR_DIR = Path(__file__).resolve().parent            # .../baostock/db
PROJECT_DIR = CUR_DIR.parent                          # .../baostock
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
from db import db_config as dbc  # noqa: E402
from db.load_local import MEMBERSHIP_FNAME, to_symbol  # noqa: E402

import baostock.security.sectorinfo as sec  # noqa: E402
import baostock.metadata.stock_metadata as meta  # noqa: E402

SECTOR_DIR = Path(config.SECTOR_DIR)

# Measured server rejection for endpoints it does not implement; not retryable.
UNSUPPORTED_ERROR_CODE = "10004020"

# name -> (callable, board_code, human label). ``board_type`` equals the name.
DATE_ENDPOINTS: Dict[str, tuple] = {
    "index_sz50": (sec.query_sz50_stocks, "sz50", "上证50成分股"),
    "index_zz500": (sec.query_zz500_stocks, "zz500", "中证500成分股"),
    "st": (sec.query_st_stocks, "st", "ST股票"),
}
# Endpoints the installed client exposes but whose server support was never confirmed: probe once,
# cache the verdict, and only loop the 51 snapshots when the answer is "supported".
PROBE_ENDPOINTS: Dict[str, tuple] = {
    "starst": (sec.query_starst_stocks, "starst", "*ST股票"),
    "terminated": (sec.query_terminated_stocks, "terminated", "终止上市"),
    "suspended": (sec.query_suspended_stocks, "suspended", "暂停上市"),
    "ame": (sec.query_ame_stocks, "ame", "中小板"),
    "szhk": (sec.query_szhk_stocks, "szhk", "深港通"),
}
# Confirmed unsupported -- listed so the omission is documented and never retried by accident.
REJECTED_ENDPOINTS: Dict[str, str] = {
    "concept": "query_stock_concept",
    "area": "query_stock_area",
    "gem": "query_gem_stocks",
    "shhk": "query_shhk_stocks",
    "risk": "query_stocks_in_risk",
}
# HS300 membership already sits in data/raw/_hs300_membership.csv -> served locally, 0 calls.
HS300_LOCAL = ("index_hs300", "hs300", "沪深300成分股")

# ``query_stock_basic`` lives in baostock.metadata.stock_metadata, not in sectorinfo -- and calling
# it with an empty ``code`` is the all-market request that hangs, hence the per-code loop below.
BASIC_FN = meta.query_stock_basic

CACHE_NAMES = {
    "industry": "industry.csv",
    "basic": "basic.csv",
    "capability": "capability.csv",
    HS300_LOCAL[0]: "index_hs300.csv",
    **{name: f"{name}.csv" for name in list(DATE_ENDPOINTS) + list(PROBE_ENDPOINTS)},
}

# Call outcomes. ``ok`` with an empty frame is meaningful (the server answered: nothing matched),
# which is why it is distinguished from ``failed`` -- only ``ok`` marks a date as attempted.
OK, UNSUPPORTED, FAILED = "ok", "unsupported", "failed"


class CallResult(NamedTuple):
    status: str
    frame: pd.DataFrame

    @property
    def ok(self) -> bool:
        return self.status == OK


# --------------------------------------------------------------------------- #
# baostock session
# --------------------------------------------------------------------------- #
class BaostockSession:
    """One baostock login shared by every call, with re-login retry and a socket timeout.

    baostock's receive loop (``socketutil.send_msg``) blocks on ``recv`` until it sees the message
    terminator and has no timeout of its own, so a stalled reply hangs the process forever. Setting
    a timeout on the shared socket turns that hang into a socket error, which ``send_msg`` converts
    into ``BSERR_RECVSOCK_FAIL`` -- something the retry below can recover from by logging in again.
    """

    def __init__(self, delay: float = 0.15, retries: int = 2, socket_timeout: int = 120):
        self.delay = delay
        self.retries = retries
        self.socket_timeout = socket_timeout
        self.calls = 0
        self.relogins = 0
        self._logged_in = False

    def __enter__(self) -> "BaostockSession":
        self.login()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.logout()

    def login(self) -> None:
        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_code} {lg.error_msg}")
        self._logged_in = True
        self._set_socket_timeout()
        logger.info(f"baostock login ok (socket timeout {self.socket_timeout}s, delay {self.delay}s)")

    def logout(self) -> None:
        import baostock as bs

        if not self._logged_in:
            return
        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass
        self._logged_in = False

    def _relogin(self) -> None:
        import baostock as bs

        self.logout()
        time.sleep(1.0)
        lg = bs.login()
        self._logged_in = lg.error_code == "0"
        if self._logged_in:
            self._set_socket_timeout()
        self.relogins += 1
        logger.warning(f"baostock re-login ({self.relogins}): {lg.error_code} {lg.error_msg}")

    def _set_socket_timeout(self) -> None:
        import baostock.common.context as ctx

        sock = getattr(ctx, "default_socket", None)
        if sock is None:
            return
        try:
            sock.settimeout(self.socket_timeout)
        except OSError as e:
            logger.warning(f"could not set the baostock socket timeout: {e}")

    def call(self, fn, **kwargs) -> CallResult:
        """Call one baostock endpoint and return ``(status, DataFrame)``."""
        empty = pd.DataFrame()
        for attempt in range(self.retries + 1):
            self.calls += 1
            try:
                rs = fn(**kwargs)
            except Exception as e:  # noqa: BLE001 - socket timeouts surface here
                logger.warning(f"{fn.__name__}({kwargs}) raised {e!r}; retry {attempt + 1}/{self.retries}")
                rs = None
            code = getattr(rs, "error_code", None)
            msg = getattr(rs, "error_msg", "") or ""
            if code == "0":
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                fields = [f for f in (rs.fields or []) if f]
                df = pd.DataFrame(rows, columns=fields) if rows else pd.DataFrame(columns=fields)
                time.sleep(self.delay)
                return CallResult(OK, df)
            if code == UNSUPPORTED_ERROR_CODE or "消息类型" in str(msg):
                logger.warning(f"{fn.__name__} is not supported by the server ({code} {msg}); skipping")
                time.sleep(self.delay)
                return CallResult(UNSUPPORTED, empty)
            logger.warning(f"{fn.__name__}({kwargs}) failed ({code} {msg}); "
                           f"retry {attempt + 1}/{self.retries}")
            self._relogin()
        time.sleep(self.delay)
        return CallResult(FAILED, empty)


# --------------------------------------------------------------------------- #
# Cache helpers (append-only, resumable)
# --------------------------------------------------------------------------- #
def _cache_path(sector_dir: Path, name: str) -> Path:
    return Path(sector_dir) / CACHE_NAMES[name]


def read_cache(path: Path) -> pd.DataFrame:
    if not (path.exists() and path.stat().st_size > 0):
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def append_cache(path: Path, df: pd.DataFrame) -> None:
    """Append to a cache CSV, writing a header when the file does not exist yet (or is empty)."""
    if df is None or df.empty:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = (not path.exists()) or path.stat().st_size == 0
    df.to_csv(path, mode="a", header=write_header, index=False, encoding="utf-8")


def _pending_codes(path: Path, codes: Sequence[str]) -> List[str]:
    cached = read_cache(path)
    done = set(cached["code"]) if "code" in cached.columns else set()
    return [c for c in codes if c not in done]


def _pending_dates(path: Path, dates: Sequence[str]) -> List[str]:
    cached = read_cache(path)
    done = set(cached["query_date"]) if "query_date" in cached.columns else set()
    return [d for d in dates if d not in done]


def _marker_row(fields: Sequence[str]) -> pd.DataFrame:
    """One empty-code row recording "this date was asked and returned nothing".

    Keeps a genuinely empty answer from being re-requested on every run. The caller adds the
    ``query_date`` column, and the loader drops rows with an empty ``code``, so the marker never
    reaches ``stock_board``.
    """
    return pd.DataFrame([{f: "" for f in fields}])


def _pending_date_plan(sector_dir: Path, name: str, dates: Sequence[str], latest_only: bool) -> List[str]:
    """Snapshot dates still missing from ``name``'s cache.

    ``latest_only`` narrows the plan to the newest snapshot, so a cache that already holds it plans
    no work at all -- otherwise the run would log into baostock only to find nothing to do.
    """
    pending = _pending_dates(_cache_path(sector_dir, name), dates)
    if latest_only and pending:
        newest = max(dates)
        pending = [d for d in pending if d == newest]
    return pending


def _optional_column(df: pd.DataFrame, name: str):
    """``df[name]`` when present, otherwise a broadcast NULL.

    baostock only returns the columns the server filled in, and a CSV cache read back with
    ``keep_default_na=False`` turns "absent" into "" -- which would land in a nullable text column
    as an empty string rather than NULL.
    """
    return df[name] if name in df.columns else None


def _blank_to_none(values: Sequence) -> list:
    """Normalise a text column so "missing" is NULL in SQL, never an empty string."""
    out = []
    for v in values:
        if v is None or (isinstance(v, float) and v != v):   # NaN
            out.append(None)
        else:
            s = str(v).strip()
            out.append(None if s == "" or s.lower() in ("nan", "nat", "none") else s)
    return out


# --------------------------------------------------------------------------- #
# Universe / snapshot dates (from the local membership cache -- 0 API calls)
# --------------------------------------------------------------------------- #
def membership(membership_path: Path = None) -> pd.DataFrame:
    membership_path = Path(membership_path or (Path(config.RAW_DIR) / MEMBERSHIP_FNAME))
    if not membership_path.exists():
        raise FileNotFoundError(f"membership cache missing: {membership_path}; run the download step first")
    m = pd.read_csv(membership_path, dtype={"code": str, "code_name": str})
    m["date"] = pd.to_datetime(m["date"], format="mixed").dt.strftime("%Y-%m-%d")
    return m


def union_codes(m: pd.DataFrame) -> List[str]:
    """All 700 baostock codes that were ever in HS300 over the cached snapshots."""
    return sorted(set(m["code"]))


def snapshot_dates(m: pd.DataFrame) -> List[str]:
    """The 51 quarter-end dates the membership cache samples."""
    return sorted(set(m["date"]))


# --------------------------------------------------------------------------- #
# Collectors
# --------------------------------------------------------------------------- #
def collect_code_endpoint(session: BaostockSession, name: str, fn, codes: Sequence[str],
                          sector_dir: Path, flush_every: int = 50, limit: int = None) -> dict:
    """Fetch a per-code endpoint (industry / basic), resuming from the cache."""
    path = _cache_path(sector_dir, name)
    todo = _pending_codes(path, codes)
    if limit:
        todo = todo[:limit]
    if not todo:
        logger.info(f"{name}: cache complete ({len(codes)} codes); 0 calls")
        return {"dataset": name, "pending": 0, "fetched": 0, "failed": 0, "rows": 0}

    logger.info(f"{name}: {len(todo)} of {len(codes)} code(s) to fetch")
    buf: List[pd.DataFrame] = []
    fetched = failed = rows = 0
    unsupported = False
    for code in tqdm(todo, desc=name, unit="code"):
        res = session.call(fn, code=code)
        if res.status == UNSUPPORTED:
            unsupported = True
            break
        if not res.ok:
            failed += 1
            continue
        fetched += 1
        if not res.frame.empty:
            buf.append(res.frame)
            rows += len(res.frame)
        if len(buf) >= flush_every:
            append_cache(path, pd.concat(buf, ignore_index=True))
            buf.clear()
    if buf:
        append_cache(path, pd.concat(buf, ignore_index=True))
    if unsupported:
        logger.warning(f"{name}: the server rejects this endpoint; {fetched} code(s) kept from the cache")
    logger.info(f"{name}: fetched {fetched}, rows {rows}, failed {failed} -> {path.name}")
    return {"dataset": name, "pending": len(todo), "fetched": fetched, "failed": failed,
            "rows": rows, "unsupported": unsupported}


def collect_date_endpoint(session: BaostockSession, name: str, fn, dates: Sequence[str],
                          sector_dir: Path, latest_only: bool = False, limit: int = None) -> dict:
    """Fetch a per-date endpoint (index membership / ST lists), resuming from the cache."""
    path = _cache_path(sector_dir, name)
    todo = _pending_dates(path, dates)
    if latest_only:
        todo = [d for d in todo if d == max(dates)]
    if limit:
        todo = todo[:limit]
    if not todo:
        logger.info(f"{name}: cache covers all {len(dates)} snapshot date(s); 0 calls")
        return {"dataset": name, "pending": 0, "fetched": 0, "failed": 0, "rows": 0}

    logger.info(f"{name}: {len(todo)} of {len(dates)} snapshot date(s) to fetch")
    buf: List[pd.DataFrame] = []
    fetched = failed = rows = 0
    unsupported = False
    fields: Optional[Sequence[str]] = None
    for d in tqdm(todo, desc=name, unit="date"):
        res = session.call(fn, date=d)
        if res.status == UNSUPPORTED:
            unsupported = True
            break
        if not res.ok:
            failed += 1
            continue
        fetched += 1
        fields = list(res.frame.columns) if fields is None else fields
        frame = res.frame.copy()
        if frame.empty:
            frame = _marker_row(fields or ["updateDate", "code", "code_name"])
        else:
            rows += len(frame)
        frame.insert(0, "query_date", d)
        buf.append(frame)
        if len(buf) >= 20:
            append_cache(path, pd.concat(buf, ignore_index=True))
            buf.clear()
    if buf:
        append_cache(path, pd.concat(buf, ignore_index=True))
    if unsupported:
        logger.warning(f"{name}: the server rejects this endpoint; {fetched} date(s) kept from the cache")
    logger.info(f"{name}: fetched {fetched} date(s), rows {rows}, failed {failed} -> {path.name}")
    return {"dataset": name, "pending": len(todo), "fetched": fetched, "failed": failed,
            "rows": rows, "unsupported": unsupported}


def collect_hs300_local(m: pd.DataFrame, sector_dir: Path) -> dict:
    """Materialise ``index_hs300.csv`` from the local membership cache -- zero baostock calls."""
    name, _, _ = HS300_LOCAL
    path = _cache_path(sector_dir, name)
    dates = snapshot_dates(m)
    todo = _pending_dates(path, dates)
    if not todo:
        logger.info(f"{name}: cache covers all {len(dates)} snapshot date(s); 0 calls (local)")
        return {"dataset": name, "pending": 0, "fetched": 0, "failed": 0, "rows": 0}
    add = m[m["date"].isin(todo)].rename(columns={"date": "query_date"})
    add = add[["query_date", "code", "code_name"]].copy()
    add.insert(1, "updateDate", "")
    append_cache(path, add)
    logger.info(f"{name}: wrote {len(add)} membership row(s) for {len(todo)} date(s) from the local cache")
    return {"dataset": name, "pending": len(todo), "fetched": len(todo), "failed": 0, "rows": len(add)}


def probe_capabilities(session: BaostockSession, sector_dir: Path) -> Dict[str, bool]:
    """Probe each unverified endpoint once and cache the verdict.

    Returns ``{name: supported}`` for every probed endpoint (cached verdicts included), so a re-run
    makes no call at all.
    """
    path = _cache_path(sector_dir, "capability")
    cached = read_cache(path)
    verdicts: Dict[str, bool] = {}
    if not cached.empty and "endpoint" in cached.columns:
        for _, r in cached.iterrows():
            verdicts[r["endpoint"]] = str(r["supported"]).lower() in ("true", "1")
    todo = [n for n in PROBE_ENDPOINTS if n not in verdicts]
    if not todo:
        logger.info(f"capability: {len(verdicts)} verdict(s) cached; 0 calls")
        return verdicts

    latest = dt.date.today().strftime("%Y-%m-%d")
    records = []
    for name in todo:
        fn, _, label = PROBE_ENDPOINTS[name]
        logger.info(f"capability: probing {name} ({label}) ...")
        res = session.call(fn, date=latest)
        supported = res.ok
        records.append({
            "endpoint": name, "api": fn.__name__, "label": label, "supported": supported,
            "error_code": "" if supported else res.status, "rows": len(res.frame),
            "probe_date": latest, "probed_at": dt.datetime.now().isoformat(timespec="seconds"),
        })
        verdicts[name] = supported
        logger.info(f"capability: {name} -> {'supported' if supported else 'NOT supported'} "
                    f"({len(res.frame)} rows)")
    append_cache(path, pd.DataFrame(records))
    return verdicts


# --------------------------------------------------------------------------- #
# Cache -> stock_board / instrument
# --------------------------------------------------------------------------- #
def board_frames(sector_dir: Path) -> pd.DataFrame:
    """Normalise every sector cache into the ``stock_board_stg`` shape."""
    frames: List[pd.DataFrame] = []

    ind = read_cache(_cache_path(sector_dir, "industry"))
    # A cache truncated by an interrupted run may be missing columns; contribute nothing rather
    # than raising out of the loader.
    if not ind.empty and {"code", "industry", "updateDate"} <= set(ind.columns):
        ind = ind[ind["industry"].fillna("") != ""].copy()
        if not ind.empty:
            frames.append(pd.DataFrame({
                "board_type": "industry",
                "board_code": ind["industry"].str.strip(),
                "board_class": _optional_column(ind, "industryClassification"),
                "symbol": ind["code"].map(to_symbol),
                "code": ind["code"],
                "code_name": _optional_column(ind, "code_name"),
                # The classification's own update date (baostock refreshes it weekly on Mondays).
                "snapshot_date": pd.to_datetime(ind["updateDate"], errors="coerce"),
            }))

    for name, board_code, label in [HS300_LOCAL] + [(n, *v[1:]) for n, v in DATE_ENDPOINTS.items()] \
            + [(n, *v[1:]) for n, v in PROBE_ENDPOINTS.items()]:
        df = read_cache(_cache_path(sector_dir, name))
        if df.empty or not {"code", "query_date"} <= set(df.columns):
            continue
        df = df[df["code"].fillna("") != ""].copy()     # drop the "asked, nothing returned" markers
        if df.empty:
            continue
        frames.append(pd.DataFrame({
            "board_type": name,
            "board_code": board_code,
            "board_class": None,
            "symbol": df["code"].map(to_symbol),
            "code": df["code"],
            "code_name": _optional_column(df, "code_name"),
            # For membership/ST lists the date we ASKED for is the meaningful snapshot date.
            "snapshot_date": pd.to_datetime(df["query_date"], errors="coerce"),
        }))
        logger.debug(f"board cache {name} ({label}): {len(df)} rows")

    if not frames:
        return pd.DataFrame(columns=["board_type", "board_code", "board_class", "symbol", "code",
                                     "code_name", "snapshot_date"])
    out = pd.concat(frames, ignore_index=True)
    out["snapshot_date"] = pd.to_datetime(out["snapshot_date"], errors="coerce")
    out = out.dropna(subset=["snapshot_date", "board_code"])
    out = out[out["board_code"].astype(str).str.strip() != ""]
    out["snapshot_date"] = out["snapshot_date"].dt.date
    out["board_class"] = _blank_to_none(out["board_class"])
    out["code_name"] = _blank_to_none(out["code_name"])
    return out.drop_duplicates(subset=["board_type", "board_code", "symbol", "snapshot_date"], keep="last")


def load_boards(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    cols = ["board_type", "board_code", "board_class", "symbol", "code", "code_name", "snapshot_date"]
    dbc.copy_frame(conn, "stock_board_stg", df, columns=cols)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stock_board (board_type, board_code, board_class, symbol, code, code_name,
                                     snapshot_date, loaded_at)
            SELECT DISTINCT ON (board_type, board_code, symbol, snapshot_date)
                   board_type, board_code, board_class, symbol, code, code_name, snapshot_date, now()
            FROM stock_board_stg
            ORDER BY board_type, board_code, symbol, snapshot_date, ctid DESC
            ON CONFLICT (board_type, board_code, symbol, snapshot_date) DO UPDATE SET
                board_class = EXCLUDED.board_class,
                code        = EXCLUDED.code,
                code_name   = EXCLUDED.code_name,
                loaded_at   = now()
            """
        )
        written = cur.rowcount
        cur.execute("TRUNCATE stock_board_stg")
    return written


def load_basic(conn, sector_dir: Path) -> int:
    """Backfill ``instrument.ipo_date/out_date/sec_type/status`` from the ``basic.csv`` cache."""
    df = read_cache(_cache_path(sector_dir, "basic"))
    if df.empty or "code" not in df.columns:
        logger.info("basic: no cache; instrument listing dates left untouched")
        return 0
    # Fill in the columns the server left out: ``pd.to_datetime(df.get("ipoDate"))`` on a missing
    # column returns a scalar NaT, and the ``.dt.date`` below would then raise AttributeError.
    for col in ("code_name", "ipoDate", "outDate", "type", "status"):
        if col not in df.columns:
            df[col] = ""
    df = df[df["code"].fillna("") != ""].drop_duplicates(subset=["code"], keep="last")
    out = pd.DataFrame({
        "code": df["code"],
        "code_name": _blank_to_none(df["code_name"]),
        "ipo_date": pd.to_datetime(df["ipoDate"], errors="coerce").dt.date,
        "out_date": pd.to_datetime(df["outDate"], errors="coerce").dt.date,
        "sec_type": pd.to_numeric(df["type"], errors="coerce").astype("Int64"),
        "status": pd.to_numeric(df["status"], errors="coerce").astype("Int64"),
    })
    if out.empty:
        logger.info("basic: cache holds no usable record; instrument listing dates left untouched")
        return 0
    dbc.copy_frame(conn, "instrument_basic_stg", out)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE instrument i SET
                code_name  = COALESCE(s.code_name, i.code_name),
                ipo_date   = s.ipo_date,
                out_date   = s.out_date,
                sec_type   = s.sec_type,
                status     = s.status,
                updated_at = now()
            FROM instrument_basic_stg s
            WHERE i.code = s.code
            """
        )
        updated = cur.rowcount
        cur.execute("TRUNCATE instrument_basic_stg")
    logger.info(f"basic: backfilled {updated} instrument row(s) from {len(out)} cached record(s)")
    return updated


def load_sector_to_db(sector_dir: Path = None, dbname: str = None) -> dict:
    """Phase 2: push every sector cache into the database."""
    sector_dir = Path(sector_dir or SECTOR_DIR)
    frames = board_frames(sector_dir)
    by_type = {k: int(v) for k, v in frames.groupby("board_type").size().items()} if not frames.empty else {}
    with dbc.connection(dbname=dbname) as conn:
        started = dt.datetime.now()
        boards = load_boards(conn, frames)
        basic = load_basic(conn, sector_dir)
        dbc.log_sync(
            conn, task="sync_sector", source="baostock", status="ok",
            rows_fetched=len(frames), rows_written=boards,
            params={"sector_dir": str(sector_dir), "board_rows_by_type": by_type,
                    "instrument_backfilled": basic},
            started_at=started,
        )
        conn.commit()
    logger.info(f"stock_board: upserted {boards} row(s); by board_type: {by_type}")
    return {"boards": boards, "instrument_backfilled": basic, "by_type": by_type}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _selected(only: str) -> set:
    """Datasets to collect; ``only`` is a comma-separated subset of the dataset names."""
    everything = {"industry", "basic", HS300_LOCAL[0], *DATE_ENDPOINTS, *PROBE_ENDPOINTS}
    if not only:
        return everything
    asked = {x.strip() for x in str(only).split(",") if x.strip()}
    unknown = asked - everything
    if unknown:
        raise ValueError(f"unknown dataset(s) {sorted(unknown)}; choose from {sorted(everything)}")
    return asked


def sync_sector(
    skip_basic: bool = False,
    skip_index_history: bool = False,
    skip_probe: bool = False,
    only: str = None,
    delay: float = 0.15,
    limit: int = None,
    sector_dir=None,
    dbname: str = None,
    socket_timeout: int = 120,
    skip_load: bool = False,
) -> dict:
    """Collect the board data (cache-first) and load it into ``stock_board``.

    ``skip_index_history`` limits the per-date endpoints to the newest snapshot (1 call each instead
    of 51). ``limit`` caps the number of codes/dates fetched, which is what a smoke run wants.
    """
    t0 = time.time()
    sector_dir = Path(sector_dir or SECTOR_DIR)
    sector_dir.mkdir(parents=True, exist_ok=True)
    m = membership()
    codes = union_codes(m)
    dates = snapshot_dates(m)
    selected = _selected(only)
    logger.info(f"universe: {len(codes)} code(s), {len(dates)} snapshot date(s) "
                f"({dates[0]} .. {dates[-1]}); datasets={sorted(selected)}")

    summary: dict = {"collect": {}, "calls": 0, "relogins": 0}
    verdicts = _cached_verdicts_map(sector_dir)

    # Work out what is still missing BEFORE logging in: a fully cached re-run must not touch baostock.
    plan: Dict[str, list] = {}
    if "industry" in selected:
        plan["industry"] = _pending_codes(_cache_path(sector_dir, "industry"), codes)
    if not skip_basic and "basic" in selected:
        plan["basic"] = _pending_codes(_cache_path(sector_dir, "basic"), codes)
    if HS300_LOCAL[0] in selected:
        plan[HS300_LOCAL[0]] = _pending_dates(_cache_path(sector_dir, HS300_LOCAL[0]), dates)
    for name in DATE_ENDPOINTS:
        if name in selected:
            plan[name] = _pending_date_plan(sector_dir, name, dates, skip_index_history)

    need_probe: List[str] = []
    for name in PROBE_ENDPOINTS:
        if name not in selected:
            continue
        if name not in verdicts:
            if skip_probe:
                logger.info(f"{name}: no cached capability verdict and skip_probe is set; skipped")
                continue
            need_probe.append(name)          # one call decides; the plan is amended once it answers
        elif verdicts[name]:
            plan[name] = _pending_date_plan(sector_dir, name, dates, skip_index_history)
        else:
            logger.info(f"{name}: rejected by the baostock server (cached verdict); skipped")
    if need_probe:
        plan["capability"] = need_probe

    # HS300 membership never needs the API.
    if HS300_LOCAL[0] in selected:
        summary["collect"][HS300_LOCAL[0]] = collect_hs300_local(m, sector_dir)

    api_work = {k: len(v) for k, v in plan.items() if k != HS300_LOCAL[0] and v}
    if api_work:
        logger.info(f"pending baostock work: {api_work}")
        with BaostockSession(delay=delay, socket_timeout=socket_timeout) as session:
            if plan.get("capability"):
                verdicts = probe_capabilities(session, sector_dir)
                for name in need_probe:
                    if verdicts.get(name):
                        plan[name] = _pending_date_plan(sector_dir, name, dates, skip_index_history)

            if plan.get("industry"):
                summary["collect"]["industry"] = collect_code_endpoint(
                    session, "industry", sec.query_stock_industry, codes, sector_dir, limit=limit)
            if plan.get("basic"):
                summary["collect"]["basic"] = collect_code_endpoint(
                    session, "basic", BASIC_FN, codes, sector_dir, limit=limit)
            for name, (fn, _, _) in DATE_ENDPOINTS.items():
                if plan.get(name):
                    summary["collect"][name] = collect_date_endpoint(
                        session, name, fn, dates, sector_dir, latest_only=skip_index_history, limit=limit)
            # Probed endpoints join the plan above, so they are collected in the same session.
            for name, (fn, _, _) in PROBE_ENDPOINTS.items():
                if plan.get(name):
                    summary["collect"][name] = collect_date_endpoint(
                        session, name, fn, dates, sector_dir, latest_only=skip_index_history, limit=limit)
            summary["calls"] = session.calls
            summary["relogins"] = session.relogins
    else:
        logger.info("nothing left to fetch from baostock; skipping the login entirely (0 calls)")

    for name, api in REJECTED_ENDPOINTS.items():
        logger.debug(f"{name} ({api}): rejected by the baostock server (10004020); out of scope")

    if not skip_load:
        summary["load"] = load_sector_to_db(sector_dir, dbname)
    summary["elapsed_sec"] = round(time.time() - t0, 1)
    logger.info(f"sync_sector done in {summary['elapsed_sec']}s with {summary['calls']} baostock call(s), "
                f"{summary['relogins']} re-login(s)")
    return summary


def _cached_verdicts_map(sector_dir: Path) -> Dict[str, bool]:
    cached = read_cache(_cache_path(sector_dir, "capability"))
    if cached.empty or "endpoint" not in cached.columns:
        return {}
    return {r["endpoint"]: str(r["supported"]).lower() in ("true", "1") for _, r in cached.iterrows()}


if __name__ == "__main__":
    import fire

    fire.Fire(sync_sector)
