"""tests/test_gap_analysis.py — full coverage of src/gap_analysis.py."""

import io
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

from src.gap_analysis import (
    parse_timestamps,
    load_target,
    check_interval,
    build_timeline,
    classify_gaps,
)


# ===========================================================================
# parse_timestamps
# ===========================================================================

class TestParseTimestamps:
    """Tests for the auto-detecting timestamp parser."""

    def test_iso_format_returned_unchanged(self):
        s = pd.Series(["2023-01-15 10:00:00", "2023-06-30 23:55:00"])
        result = parse_timestamps(s)
        assert result.notna().all()
        assert result.iloc[0] == pd.Timestamp("2023-01-15 10:00:00")

    def test_dayfirst_format_detected(self):
        # "13-01-2023" would be parsed incorrectly as month=13 by ISO mode → NaT
        s = pd.Series(["13-01-2023 08:00:00", "25-12-2022 12:00:00"])
        result = parse_timestamps(s)
        assert result.notna().all()
        assert result.iloc[0].day == 13
        assert result.iloc[0].month == 1

    def test_ambiguous_prefers_iso_when_equal_nats(self):
        # "01-02-2023" could be Jan 2 (ISO) or Feb 1 (dayfirst) — both parse; ISO wins
        s = pd.Series(["01-02-2023", "03-04-2023"])
        result = parse_timestamps(s)
        assert result.notna().all()

    def test_completely_unparseable_returns_nat(self):
        s = pd.Series(["not_a_date", "garbage"])
        result = parse_timestamps(s)
        assert result.isna().all()

    def test_mixed_valid_invalid(self):
        s = pd.Series(["2023-01-01", "bad_value", "2023-03-15"])
        result = parse_timestamps(s)
        assert result.iloc[0].year == 2023
        assert pd.isna(result.iloc[1])

    def test_empty_series(self):
        s = pd.Series([], dtype=str)
        result = parse_timestamps(s)
        assert len(result) == 0

    def test_single_element_iso(self):
        s = pd.Series(["2024-07-04 00:00:00"])
        result = parse_timestamps(s)
        assert result.iloc[0] == pd.Timestamp("2024-07-04")


# ===========================================================================
# load_target
# ===========================================================================

class TestLoadTarget:
    """Tests for load_target — CSV loading, dedup, and quality summary."""

    def test_happy_path_returns_df_and_summary(self, raw_target_csv):
        df, summary = load_target(raw_target_csv)
        assert isinstance(df, pd.DataFrame)
        assert "TimeStamp" in df.columns
        assert summary["total_rows"] == 100
        assert summary["duplicate_timestamps"] == 0
        assert summary["rows_after_dedup"] == 100

    def test_duplicates_dropped(self, tmp_path):
        tl = pd.date_range("2023-01-01", periods=5, freq="5min")
        rows = []
        for ts in tl:
            rows.append({"TimeStamp": ts.strftime("%Y-%m-%d %H:%M:%S"), "CurrentTemperature": 25.0})
        # Add a duplicate of the first timestamp
        rows.append({"TimeStamp": tl[0].strftime("%Y-%m-%d %H:%M:%S"), "CurrentTemperature": 99.0})
        df_in = pd.DataFrame(rows)
        p = tmp_path / "dup.csv"
        df_in.to_csv(p, index=False)
        df, summary = load_target(str(p))
        assert summary["duplicate_timestamps"] == 1
        assert summary["rows_after_dedup"] == 5
        # First value kept, not the duplicate
        assert df["CurrentTemperature"].iloc[0] == 25.0

    def test_future_timestamps_filtered_out(self, tmp_path):
        rows = [
            {"TimeStamp": "2023-01-01 00:00:00", "CurrentTemperature": 25.0},
            {"TimeStamp": "2031-01-01 00:00:00", "CurrentTemperature": 99.0},  # year > 2030
        ]
        p = tmp_path / "future.csv"
        pd.DataFrame(rows).to_csv(p, index=False)
        df, summary = load_target(str(p))
        assert len(df) == 1
        assert df["TimeStamp"].iloc[0].year == 2023

    def test_nat_timestamps_dropped(self, tmp_path):
        rows = [
            {"TimeStamp": "2023-01-01 00:00:00", "CurrentTemperature": 25.0},
            {"TimeStamp": "not_a_date",           "CurrentTemperature": 30.0},
        ]
        p = tmp_path / "nat.csv"
        pd.DataFrame(rows).to_csv(p, index=False)
        df, summary = load_target(str(p))
        assert len(df) == 1

    def test_timestamps_rounded_to_5min(self, tmp_path):
        rows = [
            {"TimeStamp": "2023-01-01 00:02:30", "CurrentTemperature": 25.0},
            {"TimeStamp": "2023-01-01 00:07:45", "CurrentTemperature": 26.0},
        ]
        p = tmp_path / "round.csv"
        pd.DataFrame(rows).to_csv(p, index=False)
        df, _ = load_target(str(p))
        for ts in df["TimeStamp"]:
            assert ts.minute % 5 == 0
            assert ts.second == 0

    def test_summary_keys_present(self, raw_target_csv):
        _, summary = load_target(raw_target_csv)
        for key in ["total_rows", "unique_timestamps", "duplicate_timestamps",
                    "rows_after_dedup", "date_start", "date_end"]:
            assert key in summary

    def test_sorted_output(self, tmp_path):
        # Provide data out of order
        rows = [
            {"TimeStamp": "2023-01-01 00:10:00", "CurrentTemperature": 26.0},
            {"TimeStamp": "2023-01-01 00:00:00", "CurrentTemperature": 25.0},
            {"TimeStamp": "2023-01-01 00:05:00", "CurrentTemperature": 25.5},
        ]
        p = tmp_path / "unsorted.csv"
        pd.DataFrame(rows).to_csv(p, index=False)
        df, _ = load_target(str(p))
        assert df["TimeStamp"].is_monotonic_increasing


# ===========================================================================
# check_interval
# ===========================================================================

class TestCheckInterval:
    """Tests for check_interval."""

    def _make_df(self, minutes: float, n: int = 50) -> pd.DataFrame:
        tl = pd.date_range("2023-01-01", periods=n, freq=f"{int(minutes * 60)}s")
        return pd.DataFrame({"TimeStamp": tl})

    def test_five_min_detected_ok(self):
        df = self._make_df(5)
        dominant, ok = check_interval(df)
        assert dominant == pytest.approx(5.0, abs=0.01)
        assert ok is True

    def test_ten_min_returns_false(self):
        df = self._make_df(10)
        dominant, ok = check_interval(df)
        assert dominant == pytest.approx(10.0, abs=0.01)
        assert ok is False

    def test_returns_float_and_bool(self):
        df = self._make_df(5)
        dominant, ok = check_interval(df)
        assert isinstance(dominant, float)
        assert isinstance(ok, bool)

    def test_near_five_is_ok(self):
        # slight timing jitter: 4.99 min
        tl = pd.date_range("2023-01-01", periods=50, freq="299s")
        df = pd.DataFrame({"TimeStamp": tl})
        dominant, ok = check_interval(df)
        assert ok is True


# ===========================================================================
# build_timeline
# ===========================================================================

class TestBuildTimeline:
    """Tests for build_timeline."""

    def test_timeline_covers_full_range(self, raw_target_csv):
        df, _ = load_target(raw_target_csv)
        tl, main, report = build_timeline(df)
        assert tl[0] == df["TimeStamp"].min()
        assert tl[-1] == df["TimeStamp"].max()

    def test_timeline_freq_is_5min(self, raw_target_csv):
        df, _ = load_target(raw_target_csv)
        tl, _, _ = build_timeline(df)
        diffs = pd.Series(tl).diff().dt.total_seconds().dropna()
        assert (diffs == 300).all()

    def test_main_index_is_timestamp(self, raw_target_csv):
        df, _ = load_target(raw_target_csv)
        _, main, _ = build_timeline(df)
        assert main.index.name == "TimeStamp"

    def test_missing_report_keys(self, raw_target_csv):
        df, _ = load_target(raw_target_csv)
        _, _, report = build_timeline(df)
        for k in ["expected_rows", "actual_rows", "missing_timestamps"]:
            assert k in report

    def test_no_missing_when_data_is_complete(self, raw_target_csv):
        df, _ = load_target(raw_target_csv)
        _, _, report = build_timeline(df)
        # all data was on a perfect 5-min grid so missing == 0
        assert report["missing_timestamps"] == 0

    def test_missing_count_correct_with_gaps(self):
        # Build a 5-min timeline, then drop interior rows (keep first & last so
        # the overall date range, and therefore expected_rows, stays predictable)
        tl = pd.date_range("2023-01-01", periods=30, freq="5min")
        # Keep first, last, and every 3rd interior row → the full grid is 30 slots
        keep_idx = sorted({0, 29} | set(range(0, 30, 3)))
        df = pd.DataFrame({"TimeStamp": tl[keep_idx], "v": 1.0}).reset_index(drop=True)
        _, _, report = build_timeline(df)
        assert report["expected_rows"] == 30  # first-to-last on the 5-min grid
        assert report["missing_timestamps"] > 0  # some interior rows were dropped

    def test_reindex_inserts_nans_for_missing(self):
        tl = pd.date_range("2023-01-01", periods=10, freq="5min")
        # Drop rows 3 and 7
        keep = [i for i in range(10) if i not in (3, 7)]
        df = pd.DataFrame({"TimeStamp": tl[keep], "v": 1.0}).reset_index(drop=True)
        _, main, report = build_timeline(df)
        assert main["v"].isna().sum() == 2


# ===========================================================================
# classify_gaps
# ===========================================================================

class TestClassifyGaps:
    """Tests for classify_gaps."""

    def _make_main(self, size: int = 50, gap_slices=None) -> pd.DataFrame:
        """Build a minimal reindexed main DataFrame with specified NaN slices."""
        tl = pd.date_range("2023-01-01", periods=size, freq="5min")
        df = pd.DataFrame({"CurrentTemperature": np.random.default_rng(0).uniform(20, 35, size)},
                          index=tl)
        df.index.name = "TimeStamp"
        if gap_slices:
            for s in gap_slices:
                df.iloc[s, 0] = np.nan
        return df

    def test_no_gaps(self):
        df = self._make_main(20)
        (gtm, gim, gsm, iso, med, lon, gsizes, groups, report) = classify_gaps(df, "CurrentTemperature")
        assert len(iso) == 0
        assert len(med) == 0
        assert len(lon) == 0
        assert len(gsizes) == 0

    def test_isolated_gap_classified(self):
        df = self._make_main(30, [slice(10, 11)])  # 1 row gap
        _, _, _, iso, med, lon, gsizes, groups, report = classify_gaps(df, "CurrentTemperature")
        assert len(iso) == 1
        assert len(med) == 0
        assert len(lon) == 0

    def test_medium_gap_classified(self):
        df = self._make_main(40, [slice(15, 18)])  # 3-row gap
        _, _, _, iso, med, lon, _, _, _ = classify_gaps(df, "CurrentTemperature")
        assert len(med) == 3
        assert len(iso) == 0

    def test_long_gap_classified(self):
        df = self._make_main(50, [slice(20, 32)])  # 12-row gap
        _, _, _, iso, med, lon, _, _, _ = classify_gaps(df, "CurrentTemperature")
        assert len(lon) == 12
        assert len(iso) == 0

    def test_boundary_medium_exactly_5(self):
        df = self._make_main(30, [slice(10, 15)])  # exactly 5 rows → medium
        _, _, _, iso, med, lon, _, _, _ = classify_gaps(df, "CurrentTemperature")
        assert len(med) == 5

    def test_boundary_long_exactly_6(self):
        df = self._make_main(30, [slice(10, 16)])  # exactly 6 rows → long
        _, _, _, iso, med, lon, _, _, _ = classify_gaps(df, "CurrentTemperature")
        assert len(lon) == 6

    def test_gap_type_map_populated(self):
        df = self._make_main(30, [slice(5, 6), slice(15, 18)])
        gtm, _, _, _, _, _, _, _, _ = classify_gaps(df, "CurrentTemperature")
        # isolated at row 5 → 'isolated'
        iso_ts = df.index[5]
        assert gtm[iso_ts] == "isolated"
        # medium rows 15-17 → 'medium'
        med_ts = df.index[15]
        assert gtm[med_ts] == "medium"

    def test_gap_size_map_correct(self):
        df = self._make_main(30, [slice(10, 13)])  # 3-row gap
        _, _, gsm, _, _, _, _, _, _ = classify_gaps(df, "CurrentTemperature")
        for ts in df.index[10:13]:
            assert gsm[ts] == 3

    def test_gap_report_shape(self):
        df = self._make_main(40, [slice(5, 6), slice(20, 23), slice(30, 40)])
        *_, report = classify_gaps(df, "CurrentTemperature")
        assert list(report["gap_type"]) == ["no_gap", "isolated", "medium", "long"]

    def test_multiple_gap_types_together(self):
        df = self._make_main(60, [slice(5, 6), slice(20, 23), slice(40, 52)])
        _, _, _, iso, med, lon, _, _, _ = classify_gaps(df, "CurrentTemperature")
        assert len(iso) == 1
        assert len(med) == 3
        assert len(lon) == 12
