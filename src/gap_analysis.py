"""gap_analysis.py — load, clean, timeline, and classify gaps (notebook Steps 1-4)."""

from typing import Tuple, Dict, Set
import pandas as pd
import numpy as np


def parse_timestamps(series: pd.Series) -> pd.Series:
    """Parse timestamps, auto-detecting ISO (YYYY-MM-DD) vs day-first (DD-MM-YYYY).

    Picks whichever interpretation leaves fewer NaT failures.
    """
    iso = pd.to_datetime(series, errors='coerce')
    dayfirst = pd.to_datetime(series, dayfirst=True, errors='coerce')
    if iso.isna().sum() <= dayfirst.isna().sum():
        return iso
    return dayfirst


def load_target(target_file: str) -> Tuple[pd.DataFrame, Dict]:
    """Load target CSV, round to 5-min, dedup, sort; return frame + quality summary."""
    df_raw = pd.read_csv(target_file)
    df_raw['TimeStamp'] = parse_timestamps(df_raw['TimeStamp'])
    df_raw = df_raw[df_raw['TimeStamp'].notna()]
    df_raw = df_raw[df_raw['TimeStamp'].dt.year <= 2030]

    df_raw['TimeStamp'] = df_raw['TimeStamp'].dt.round('5min')
    df_raw = df_raw.sort_values('TimeStamp')

    total_rows = len(df_raw)
    unique_ts = df_raw['TimeStamp'].nunique()
    dup_count = total_rows - unique_ts
    df_raw = df_raw.drop_duplicates('TimeStamp', keep='first').reset_index(drop=True)

    quality_summary = {
        'total_rows': total_rows,
        'unique_timestamps': unique_ts,
        'duplicate_timestamps': dup_count,
        'rows_after_dedup': len(df_raw),
        'date_start': df_raw['TimeStamp'].min(),
        'date_end': df_raw['TimeStamp'].max(),
    }

    print(f"Total Rows           : {total_rows:,}")
    print(f"Unique Timestamps    : {unique_ts:,}")
    print(f"Duplicate Timestamps : {dup_count:,}")
    print(f"Rows after dedup     : {len(df_raw):,}")
    print(f"Date range           : {quality_summary['date_start']} -> {quality_summary['date_end']}")

    return df_raw, quality_summary


def check_interval(df_raw: pd.DataFrame) -> Tuple[float, bool]:
    """Compute dominant reporting interval (minutes) and confirm it is ~5 min."""
    intervals = df_raw['TimeStamp'].diff().dt.total_seconds().div(60).dropna()
    interval_dist = intervals.value_counts().sort_values(ascending=False)
    print("Interval distribution (minutes):")
    print(interval_dist.head(8).to_string())

    dominant = float(intervals.mode()[0])
    ok_flag = abs(dominant - 5) < 0.1
    print(f"\nDominant interval: {dominant} min")
    if ok_flag:
        print("OK — 5-minute sensor confirmed. Continuing.")
    else:
        print("WARNING — dominant interval is not 5 minutes.")

    return dominant, ok_flag


def build_timeline(df_raw: pd.DataFrame) -> Tuple[pd.DatetimeIndex, pd.DataFrame, Dict]:
    """Build fixed 5-min timeline, reindex target onto it; return grid, frame, report."""
    timeline = pd.date_range(df_raw['TimeStamp'].min(),
                             df_raw['TimeStamp'].max(), freq='5min')
    expected_rows = len(timeline)
    actual_rows = len(df_raw)
    missing_count = expected_rows - actual_rows

    main = df_raw.set_index('TimeStamp').reindex(timeline)
    main.index.name = 'TimeStamp'

    missing_report = {
        'expected_rows': expected_rows,
        'actual_rows': actual_rows,
        'missing_timestamps': missing_count,
    }

    print(f"Expected Rows (5-min grid): {expected_rows:,}")
    print(f"Actual Rows               : {actual_rows:,}")
    print(f"Missing Timestamps        : {missing_count:,} "
          f"({100 * missing_count / expected_rows:.1f}%)")

    return timeline, main, missing_report


def classify_gaps(
    main: pd.DataFrame, ref_col: str
) -> Tuple[Dict, Dict, Dict, Set, Set, Set, pd.Series, pd.Series, pd.DataFrame]:
    """Classify consecutive missing blocks; isolated=1, medium=2-5, long=>5 (no_gap=0)."""
    is_miss = main[ref_col].isna()
    groups = (is_miss != is_miss.shift()).cumsum()[is_miss]
    gsizes = groups.value_counts()

    gap_type_map: Dict = {}
    gap_id_map: Dict = {}
    gap_size_map: Dict = {}
    iso_idx: Set = set()
    med_idx: Set = set()
    long_idx: Set = set()

    for gid, size in gsizes.items():
        idx_set = set(groups[groups == gid].index)
        if size == 1:
            gtype, bkt = 'isolated', iso_idx
        elif size <= 5:
            gtype, bkt = 'medium', med_idx
        else:
            gtype, bkt = 'long', long_idx
        bkt.update(idx_set)
        for ts in idx_set:
            gap_type_map[ts] = gtype
            gap_id_map[ts] = int(gid)
            gap_size_map[ts] = size

    gap_report = pd.DataFrame({
        'gap_type': ['no_gap', 'isolated', 'medium', 'long'],
        'blocks': [int(main[ref_col].notna().sum()),
                   int((gsizes == 1).sum()),
                   int(((gsizes >= 2) & (gsizes <= 5)).sum()),
                   int((gsizes > 5).sum())],
        'rows': [int(main[ref_col].notna().sum()),
                 int((gsizes == 1).sum()),
                 int(gsizes[(gsizes >= 2) & (gsizes <= 5)].sum()),
                 int(gsizes[gsizes > 5].sum())],
    })
    print("Gap Report:")
    print(gap_report.to_string(index=False))
    if len(gsizes):
        print(f"\nLargest gap: {gsizes.max()} rows = {gsizes.max() * 5} min")

    return (gap_type_map, gap_id_map, gap_size_map,
            iso_idx, med_idx, long_idx, gsizes, groups, gap_report)
