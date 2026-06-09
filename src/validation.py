"""validation.py — rainfall, continuity, audit sync, evaluation (notebook Steps 13-17)."""

from typing import Dict, List, Tuple
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


def fill_rainfall_conservative(main: pd.DataFrame, neighbor_ids: List[str]) -> pd.DataFrame:
    """Fill rainfall NaNs with 0 only when the local window AND neighbours are all dry."""
    rain_vars = [c for c in ['RainfallHourly', 'RainfallDaily', 'RainfallWeekly']
                 if c in main.columns]

    for col in rain_vars:
        filled = 0
        for ts in main.index[main[col].isna()]:
            pos = main.index.get_loc(ts)
            win = main.iloc[max(0, pos - 3):min(len(main), pos + 4)]
            nbr = [main.loc[ts, f'RainH_{nid}'] for nid in neighbor_ids
                   if f'RainH_{nid}' in main.columns]
            nbr_dry = all(pd.isna(v) or v == 0 for v in nbr)
            if win[col].fillna(0).max() == 0 and nbr_dry:
                main.loc[ts, col] = 0
                filled += 1
        print(f"  {col}: {filled:,} rows set to 0 (rest left unchanged)")

    return main


def validate_continuity(
    main: pd.DataFrame, jump_thresholds: Dict[str, float]
) -> Tuple[pd.DataFrame, Dict[str, int]]:
    """Flag unrealistic 5-min jumps in reconstructed rows and mildly correct them.

    Original observations are never modified.
    """
    non_orig = main['imputation_method'] != 'original'
    continuity_report: Dict[str, int] = {}

    for col, thresh in jump_thresholds.items():
        if col not in main.columns:
            continue
        dprev = (main[col] - main[col].shift(1)).abs()
        dnext = (main[col] - main[col].shift(-1)).abs()
        bad = non_orig & ((dprev > thresh) | (dnext > thresh))
        continuity_report[col] = int(bad.sum())
        for ts in main.index[bad]:
            pos = main.index.get_loc(ts)
            nb_vals = [main.iloc[pos - 1][col] if pos > 0 else np.nan,
                       main.iloc[pos + 1][col] if pos < len(main) - 1 else np.nan]
            nb_vals = [v for v in nb_vals if not pd.isna(v)]
            if nb_vals:
                main.loc[ts, col] = 0.6 * main.loc[ts, col] + 0.4 * np.mean(nb_vals)

    print("Continuity flags (reconstructed rows only):")
    for k, v in continuity_report.items():
        print(f"  {k}: {v}")

    return main, continuity_report


def sync_audit_columns(main: pd.DataFrame) -> pd.DataFrame:
    """Sync filled_flag to imputation_method and print method/confidence summaries."""
    main['filled_flag'] = np.where(main['imputation_method'] == 'original', 0, 1)

    print("Audit columns present:",
          ['filled_flag', 'imputation_method', 'confidence_level', 'gap_type', 'gap_size', 'gap_id'])
    print("\nMethod summary:")
    print(main['imputation_method'].value_counts().to_string())
    print("\nConfidence summary:")
    print(main['confidence_level'].value_counts().to_string())

    return main


def evaluate_synthetic_gaps(
    main: pd.DataFrame,
    cont_vars: List[str],
    neighbor_ids: List[str],
    model_selection: Dict,
    timeline: pd.DatetimeIndex,
    gap_sizes: List[int],
    jump_thresholds: Dict[str, float],
    clip: Dict[str, Tuple[float, float]],
    split_frac: float = 0.80,
    n_gaps: int = 150,
    seed: int = 42,
) -> pd.DataFrame:
    """Evaluate reconstruction via synthetic gap masking on a chronological split.

    Trains each variable's selected model on the first `split_frac` of the timeline,
    masks whole synthetic gaps (sizes in `gap_sizes`) in the held-out tail, and reports
    MAE / RMSE / R2 / Bias per variable and gap size. Features use only pre-gap
    information (ffill lags + rolling) to avoid future leakage.
    """
    n_total = len(timeline)
    cutoff = int(split_frac * n_total)
    train_end_ts = timeline[cutoff - 1]
    test_start_ts = timeline[cutoff]
    omask = main['imputation_method'] == 'original'
    test_orig_idx = main.index[omask & (main.index >= test_start_ts)]

    def gen_gaps(orig_idx, gap_size, n_gaps=n_gaps, seed=seed):
        rng = np.random.default_rng(seed)
        oset = set(orig_idx)
        sidx = sorted(orig_idx)
        cand = []
        for i in range(len(sidx) - gap_size):
            block = sidx[i:i + gap_size]
            if any((block[j + 1] - block[j]).seconds // 60 != 5 for j in range(len(block) - 1)):
                continue
            pb = timeline.get_loc(block[0]) - 1
            pa = timeline.get_loc(block[-1]) + 1
            if pb < 0 or pa >= len(timeline):
                continue
            if timeline[pb] not in oset or timeline[pa] not in oset:
                continue
            cand.append((timeline[pb], block, timeline[pa]))
        if not cand:
            return []
        pick = rng.choice(len(cand), size=min(n_gaps, len(cand)), replace=False)
        return [cand[i] for i in pick]

    def eval_features(col, visible):
        f = pd.DataFrame(index=main.index)
        f['hour'] = main.index.hour
        f['day_of_week'] = main.index.dayofweek
        f['month'] = main.index.month
        known = visible.ffill()
        f['lag_1'] = known.shift(1)
        f['lag_2'] = known.shift(2)
        f['lag_3'] = known.shift(3)
        f['rolling_mean'] = known.shift(1).rolling(6, min_periods=3).mean()
        f['rolling_std'] = known.shift(1).rolling(6, min_periods=3).std()
        for nid in neighbor_ids:
            for s in ['Temp', 'CorrTemp', 'Hum', 'CorrHum', 'Pres', 'Wind']:
                c = f'{s}_{nid}'
                if c in main.columns:
                    f[c] = main[c]
        return f

    eval_records = []
    for col in cont_vars:
        if col not in model_selection:
            continue
        vis_train = main[col].copy()
        vis_train[main.index > train_end_ts] = np.nan
        ft = eval_features(col, vis_train)
        tok = (omask & (main.index <= train_end_ts) & main[col].notna() & ft.notna().all(axis=1))
        if tok.sum() < 100:
            continue

        em = type(model_selection[col])(**model_selection[col].get_params())
        em.fit(ft[tok], main.loc[tok, col])

        for gs in gap_sizes:
            gaps = gen_gaps(test_orig_idx, gs, n_gaps=n_gaps)
            yt, yp = [], []
            for tsb, block, tsa in gaps:
                tv = main.loc[block, col].values
                if np.any(np.isnan(tv)):
                    continue
                vis = main[col].copy()
                vis[list(block)] = np.nan
                fb = eval_features(col, vis).loc[block].dropna()
                if fb.empty:
                    continue
                raw = em.predict(fb)
                pv = main.loc[tsb, col]
                nv = main.loc[tsa, col]
                if not pd.isna(pv) and not pd.isna(nv):
                    lo = min(pv, nv) - jump_thresholds.get(col, 5)
                    hi = max(pv, nv) + jump_thresholds.get(col, 5)
                    raw = np.clip(raw, lo, hi)
                raw = np.clip(raw, *clip[col])
                for k, ts in enumerate(fb.index):
                    yt.append(tv[list(block).index(ts)])
                    yp.append(raw[k])
            if len(yt) < 5:
                continue
            yt = np.array(yt)
            yp = np.array(yp)
            eval_records.append({
                'variable': col, 'gap_size': gs, 'n_samples': len(yt),
                'MAE': round(mean_absolute_error(yt, yp), 4),
                'RMSE': round(np.sqrt(mean_squared_error(yt, yp)), 4),
                'R2': round(r2_score(yt, yp), 4),
                'Bias': round(float(np.mean(yp - yt)), 4),
            })

    evaluation_df = pd.DataFrame(eval_records)
    print("Evaluation Report (synthetic gaps, chronological split):")
    print(evaluation_df.to_string(index=False))

    return evaluation_df


def plot_mae_vs_gapsize(
    evaluation_df: pd.DataFrame,
    cont_vars: List[str],
    gap_sizes: List[int],
    output_dir: str = ".",
) -> None:
    """Plot MAE vs gap size per variable and save to output_dir."""
    fig, ax = plt.subplots(figsize=(10, 5))
    for col in cont_vars:
        rows = evaluation_df[evaluation_df['variable'] == col]
        if rows.empty:
            continue
        ax.plot(rows['gap_size'], rows['MAE'], marker='o', label=col)
    ax.set_xlabel('Gap Size (rows)')
    ax.set_ylabel('MAE')
    ax.set_xticks(gap_sizes)
    ax.set_title('Reconstruction MAE vs Gap Size')
    ax.legend(fontsize=8, ncol=2)
    ax.grid(True, alpha=0.4)
    plt.tight_layout()
    plt.savefig(f"{output_dir}/mae_vs_gapsize.png", dpi=120, bbox_inches='tight')
    plt.close()
