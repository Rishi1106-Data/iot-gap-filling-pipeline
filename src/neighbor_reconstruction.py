"""neighbor_reconstruction.py — neighbour loading, reliability, long-gap fill (Steps 5, 6, 11)."""

from typing import Dict, List, Set, Tuple
import pandas as pd
import numpy as np

from gap_analysis import parse_timestamps

# Composite environmental quality reference (replaces single CurrentTemperature).
# Quality/coverage/reliability are measured across these corrected variables.
QUALITY_REFERENCE_VARS = ["CorrectedTemp", "CorrectedHumidity", "CorrectedHeatIndex"]
FALLBACK_REFERENCE = "CurrentTemperature"

# Short prefixes used when corrected columns are attached to `main` as neighbour features.
_QUALITY_SHORT = {"CorrectedTemp": "CorrTemp",
                  "CorrectedHumidity": "CorrHum",
                  "CorrectedHeatIndex": "CorrHeatIdx"}


def classify_gaps_series(series: pd.Series) -> Tuple[int, int, int]:
    """Count isolated / medium / long gap blocks in a single neighbour series."""
    m = series.isna()
    g = (m != m.shift()).cumsum()[m]
    s = g.value_counts()
    return (int((s == 1).sum()),
            int(((s >= 2) & (s <= 5)).sum()),
            int((s > 5).sum()))


def load_neighbors(
    neighbor_files: List[str],
    neighbor_ids: List[str],
    timeline: pd.DatetimeIndex,
    main: pd.DataFrame,
) -> Tuple[Dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame]:
    """Load each neighbour, align to grid, attach feature columns, build quality report."""
    neighbors: Dict[str, pd.DataFrame] = {}
    neighbor_quality: List[Dict] = []

    for fname, nid in zip(neighbor_files, neighbor_ids):
        nd = pd.read_csv(fname)
        nd['TimeStamp'] = parse_timestamps(nd['TimeStamp'])
        nd = nd[nd['TimeStamp'].notna()]
        nd = nd[nd['TimeStamp'].dt.year <= 2030]
        nd['TimeStamp'] = nd['TimeStamp'].dt.round('5min')
        nd = nd.drop_duplicates('TimeStamp').sort_values('TimeStamp')
        nd = nd.set_index('TimeStamp').reindex(timeline)
        neighbors[nid] = nd

        # Composite corrected coverage: per-variable + average; fall back to CurrentTemperature
        n_slots = len(timeline)
        per_cov = {}
        for v in QUALITY_REFERENCE_VARS:
            per_cov[v] = (round(100 * nd[v].notna().sum() / n_slots, 2)
                          if v in nd.columns else np.nan)

        present_corrected = [v for v in QUALITY_REFERENCE_VARS if v in nd.columns]
        if present_corrected:
            avg_cov = round(float(np.mean([per_cov[v] for v in present_corrected])), 2)
            avail = nd[present_corrected].notna().any(axis=1)
        else:
            avail = nd[FALLBACK_REFERENCE].notna()
            avg_cov = round(100 * avail.sum() / n_slots, 2)

        ref_series = pd.Series(np.where(avail, 1.0, np.nan), index=nd.index)
        present = int(avail.sum())
        iso_n, med_n, long_n = classify_gaps_series(ref_series)
        neighbor_quality.append({
            'neighbor': nid,
            'rows_present': present,
            'coverage_pct': avg_cov,
            'missing_pct': round(100 - avg_cov, 2),
            'isolated_gaps': iso_n,
            'medium_gaps': med_n,
            'long_gaps': long_n,
            'corrected_temp_coverage': per_cov['CorrectedTemp'],
            'corrected_humidity_coverage': per_cov['CorrectedHumidity'],
            'corrected_heatindex_coverage': per_cov['CorrectedHeatIndex'],
            'avg_corrected_coverage': avg_cov,
            'date_start': ref_series.dropna().index.min(),
            'date_end': ref_series.dropna().index.max(),
        })

        for orig, short in {'CurrentTemperature': 'Temp', 'CorrectedTemp': 'CorrTemp',
                            'CurrentHumidity': 'Hum', 'CorrectedHumidity': 'CorrHum',
                            'CorrectedHeatIndex': 'CorrHeatIdx',
                            'AtmPressure': 'Pres', 'WindSpeed': 'Wind',
                            'RainfallHourly': 'RainH'}.items():
            if orig in nd.columns:
                main[f'{short}_{nid}'] = nd[orig]

    neighbor_quality_df = pd.DataFrame(neighbor_quality)
    print("Neighbor Quality Report:")
    print(neighbor_quality_df.to_string(index=False))

    return neighbors, main, neighbor_quality_df


def compute_reliability(
    main: pd.DataFrame,
    neighbor_ids: List[str],
    neighbor_distances_km: List[float],
    ref_col: str,
) -> Tuple[Dict[str, float], pd.DataFrame]:
    """Score each neighbour: 0.6 * coverage + 0.4 * overlap-with-target-missing-slots."""
    target_present = main[ref_col].notna()

    reliability: Dict[str, float] = {}
    for nid in neighbor_ids:
        # Average availability across attached corrected columns; fall back to CurrentTemperature
        avail_cols = [f'{_QUALITY_SHORT[v]}_{nid}'
                      for v in QUALITY_REFERENCE_VARS
                      if f'{_QUALITY_SHORT[v]}_{nid}' in main.columns]
        if avail_cols:
            nbr_avail = main[avail_cols].notna().mean(axis=1)
            coverage = nbr_avail.mean()
            nbr_present = nbr_avail > 0
        else:
            nbr_present = main[f'Temp_{nid}'].notna()
            coverage = nbr_present.mean()
        target_miss = ~target_present
        overlap = (nbr_present & target_miss).sum() / max(target_miss.sum(), 1)
        score = 0.6 * coverage + 0.4 * overlap
        reliability[nid] = round(score, 4)

    rel_df = pd.DataFrame({
        'neighbor': list(reliability.keys()),
        'reliability': list(reliability.values()),
        'distance_km': neighbor_distances_km,
    })
    print("Neighbor Reliability:")
    print(rel_df.to_string(index=False))

    return reliability, rel_df


def reconstruct_long_gaps(
    main: pd.DataFrame,
    neighbors: Dict[str, pd.DataFrame],
    reliability: Dict[str, float],
    neighbor_ids: List[str],
    neighbor_distances_km: List[float],
    cont_vars: List[str],
    timeline: pd.DatetimeIndex,
    groups: pd.Series,
    gsizes: pd.Series,
    clip: Dict[str, Tuple[float, float]],
) -> pd.DataFrame:
    """Fill long (>5-row) gaps via reliability+distance+overlap weighted neighbour blending.

    Neighbour trajectory is boundary-anchored to the target's own pre/post values.
    Insufficient overlap leaves the rows NaN (unresolved). Isolated/medium gaps untouched.
    """
    inv_dist = {nid: 1.0 / max(d, 0.001) for nid, d in zip(neighbor_ids, neighbor_distances_km)}

    def neighbor_weight(nid: str) -> float:
        return reliability[nid] * inv_dist[nid]

    def reconstruct_long(gap_ts: List, col: str) -> Tuple[np.ndarray, str, str]:
        block_ts = sorted(gap_ts)
        sp = timeline.get_loc(block_ts[0])
        ep = timeline.get_loc(block_ts[-1])
        size = len(block_ts)
        p = main.iloc[sp - 1][col] if sp > 0 else np.nan
        n = main.iloc[ep + 1][col] if ep < len(timeline) - 1 else np.nan

        recs, wts = {}, {}
        for nid in neighbor_ids:
            nd = neighbors[nid]
            if col not in nd.columns:
                continue
            npv = nd.iloc[sp - 1][col] if sp > 0 else np.nan
            nnv = nd.iloc[ep + 1][col] if ep < len(timeline) - 1 else np.nan
            ng = nd.loc[block_ts, col].values
            cov = int(np.sum(~np.isnan(ng)))
            if cov == 0:
                continue
            nf = np.concatenate([[npv], ng, [nnv]])
            v = ng[~np.isnan(ng)]
            if np.isnan(nf[0]):
                nf[0] = v[0] if len(v) else np.nan
            if np.isnan(nf[-1]):
                nf[-1] = v[-1] if len(v) else np.nan
            if np.isnan(nf[0]) or np.isnan(nf[-1]):
                continue
            ni = pd.Series(nf).interpolate().values
            mn, mx = ni.min(), ni.max()
            if not np.isnan(p) and not np.isnan(n):
                fracs = (np.linspace(0, 1, size + 2) if abs(mx - mn) < 0.3
                         else (ni - mn) / (mx - mn))
                recon = p + fracs[1:-1] * (n - p)
            else:
                recon = ni[1:-1]
            recs[nid] = recon
            wts[nid] = neighbor_weight(nid) * (cov / size)

        if not recs:
            return np.full(size, np.nan), 'unresolved', 'unknown'

        tw = sum(wts.values())
        blended = sum(recs[k] * wts[k] / tw for k in recs)
        blended = np.clip(blended, *clip[col])
        conf = 'Medium' if len(recs) == 2 else 'Low'
        return blended, 'neighbor', conf

    for gid, size in gsizes.items():
        if size <= 5:
            continue
        gap_ts = groups[groups == gid].index.tolist()
        for col in cont_vars:
            vals, method, conf = reconstruct_long(gap_ts, col)
            for i, ts in enumerate(sorted(gap_ts)):
                if not np.isnan(vals[i]):
                    main.loc[ts, col] = vals[i]
                    main.loc[ts, 'imputation_method'] = method
                    main.loc[ts, 'confidence_level'] = conf
                    main.loc[ts, 'filled_flag'] = 1

    print("Long gaps reconstructed.")
    print(f"Neighbor-filled rows: {(main['imputation_method'] == 'neighbor').sum():,}")
    print(f"Still unresolved (insufficient overlap): "
          f"{(main['imputation_method'] == 'unresolved').sum():,}")

    return main
