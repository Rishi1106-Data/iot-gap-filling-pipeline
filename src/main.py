"""main.py — pipeline orchestrator.

Wires the module functions together in notebook order. Contains no reconstruction
logic: every computation lives in gap_analysis / interpolation / model_selection /
neighbor_reconstruction / validation. This file only loads config, sequences calls,
logs progress, handles errors, and saves outputs.
"""

import os
import logging
from typing import Dict, Tuple

import pandas as pd

from src import gap_analysis as ga
from src import interpolation as ip
from src import neighbor_reconstruction as nr
from src import model_selection as ms
from src import validation as val

logger = logging.getLogger("iot_pipeline")


def _configure_logging() -> None:
    """Attach a basic stream handler once."""
    if not logger.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%H:%M:%S",
        )


def save_outputs(
    final_df: pd.DataFrame,
    audit_df: pd.DataFrame,
    evaluation_df: pd.DataFrame,
    neighbor_quality_df: pd.DataFrame,
    model_selection_df: pd.DataFrame,
    output_paths: Dict,
) -> None:
    """Write the five report CSVs to the configured paths."""
    os.makedirs(output_paths['output_dir'], exist_ok=True)
    final_df.reset_index().to_csv(output_paths['filled_dataset'], index=False)
    audit_df.to_csv(output_paths['audit_report'], index=False)
    evaluation_df.to_csv(output_paths['evaluation_report'], index=False)
    neighbor_quality_df.to_csv(output_paths['neighbor_quality_report'], index=False)
    if model_selection_df is not None:
        model_selection_df.to_csv(output_paths['model_selection_report'], index=False)


def _build_audit_df(main: pd.DataFrame, cont_vars, rain_vars) -> pd.DataFrame:
    """Assemble the per-variable audit summary frame (counts by method)."""
    rows = []
    for col in list(cont_vars) + list(rain_vars):
        if col in main.columns:
            rows.append({
                'variable': col,
                'original': int((main['imputation_method'] == 'original').sum()),
                'interpolation': int((main['imputation_method'] == 'interpolation').sum()),
                'model': int((main['imputation_method'] == 'model').sum()),
                'neighbor': int((main['imputation_method'] == 'neighbor').sum()),
                'still_missing': int(main[col].isna().sum()),
            })
    return pd.DataFrame(rows)


def _finalize_df(main: pd.DataFrame, neighbor_ids) -> pd.DataFrame:
    """Drop neighbour helper columns and order tracking columns last."""
    spatial_drop = []
    for nid in neighbor_ids:
        spatial_drop += [c for c in main.columns if c.endswith(f'_{nid}')]
    final_df = main.drop(columns=spatial_drop)
    tracking = ['gap_type', 'gap_id', 'gap_size', 'filled_flag',
                'imputation_method', 'confidence_level']
    other = [c for c in final_df.columns if c not in tracking]
    return final_df[other + tracking]


def run_pipeline(config: Dict) -> Tuple[pd.DataFrame, pd.DataFrame, Dict]:
    """Run the full gap-filling pipeline from a parsed config dict.

    Returns (filled_dataset, evaluation_report, execution_summary).
    """
    _configure_logging()

    # ── unpack config ──────────────────────────────────────────────────────────
    ref_col = config['reference']['ref_col']
    cont_vars = config['continuous_variables']
    clip = {k: tuple(v) for k, v in config['clipping_ranges'].items()}
    jump_thresholds = config['jump_thresholds']
    neighbor_ids = config['neighbor_distances']['ids']
    neighbor_distances_km = config['neighbor_distances']['distances_km']
    quality_reference = config['neighbor']['quality_reference']
    target_file = config['input_paths']['target_file']
    neighbor_files = config['input_paths']['neighbor_files']
    output_paths = config['output_paths']
    train_mode = config['execution']['train_mode']
    model_dir = config['models']['model_dir']
    n_splits = config['models']['n_splits']
    model_params = config.get('model_params')
    gap_sizes = config['validation']['gap_sizes']
    split_frac = config['validation']['split_fraction']
    n_gaps = config['validation']['n_gaps']
    seed = config['validation']['seed']

    execution_summary: Dict = {'train_mode': train_mode, 'target_file': target_file}

    try:
        # ── load target + gap analysis ──────────────────────────────────────────
        logger.info("Loading target dataset: %s", target_file)
        df_raw, quality_summary = ga.load_target(target_file)
        ga.check_interval(df_raw)
        timeline, main, missing_report = ga.build_timeline(df_raw)

        logger.info("Classifying gaps (ref_col=%s)", ref_col)
        (gap_type_map, gap_id_map, gap_size_map,
         iso_idx, med_idx, long_idx,
         gsizes, groups, gap_report) = ga.classify_gaps(main, ref_col)

        # ── audit columns ─────────────────────────────────────────────────────
        main = ip.init_tracking_columns(main, ref_col, gap_type_map, gap_id_map, gap_size_map)

        # ── neighbours (must precede reliability + feature use) ─────────────────
        if not (len(neighbor_files) == len(neighbor_ids) == len(neighbor_distances_km)):
            raise ValueError(
                "Neighbor config mismatch: "
                f"neighbor_files={len(neighbor_files)}, "
                f"neighbor_ids={len(neighbor_ids)}, "
                f"neighbor_distances_km={len(neighbor_distances_km)} must be equal.")

        # Drive neighbour quality/reliability reference from config (not just ref_col)
        nr.QUALITY_REFERENCE_VARS = quality_reference

        logger.info("Loading %d neighbours", len(neighbor_ids))
        neighbors, main, neighbor_quality_df = nr.load_neighbors(
            neighbor_files, neighbor_ids, timeline, main)
        reliability, rel_df = nr.compute_reliability(
            main, neighbor_ids, neighbor_distances_km, ref_col)

        # ── isolated gaps ───────────────────────────────────────────────────────
        logger.info("Filling isolated gaps")
        main = ip.fill_isolated_gaps(main, cont_vars, iso_idx, clip, ref_col)

        # ── models (train or inference) ─────────────────────────────────────────
        logger.info("Preparing models (train_mode=%s)", train_mode)
        model_selection, model_selection_df = ms.prepare_models(
            main, cont_vars, neighbor_ids, train_mode, model_dir, model_params, n_splits)

        # ── medium gaps ───────────────────────────────────────────────────────
        logger.info("Filling medium gaps")
        main = ms.fill_medium_gaps(
            main, cont_vars, model_selection, neighbor_ids, groups, gsizes, clip)

        # ── long gaps (neighbour reconstruction) ────────────────────────────────
        logger.info("Reconstructing long gaps from neighbours")
        main = nr.reconstruct_long_gaps(
            main, neighbors, reliability, neighbor_ids, neighbor_distances_km,
            cont_vars, timeline, groups, gsizes, clip)

        # ── validation ──────────────────────────────────────────────────────────
        os.makedirs(output_paths['output_dir'], exist_ok=True)
        logger.info("Validation: rainfall, continuity, audit sync")
        main = val.fill_rainfall_conservative(main, neighbor_ids)
        main, continuity_report = val.validate_continuity(main, jump_thresholds)
        main = val.sync_audit_columns(main)

        logger.info("Evaluating reconstruction (synthetic gaps)")
        evaluation_df = val.evaluate_synthetic_gaps(
            main, cont_vars, neighbor_ids, model_selection, timeline,
            gap_sizes, jump_thresholds, clip, split_frac, n_gaps, seed)
        val.plot_mae_vs_gapsize(evaluation_df, cont_vars, gap_sizes, output_paths['output_dir'])

        # ── save outputs ────────────────────────────────────────────────────────
        rain_vars = [c for c in ['RainfallHourly', 'RainfallDaily', 'RainfallWeekly']
                     if c in main.columns]
        audit_df = _build_audit_df(main, cont_vars, rain_vars)
        final_df = _finalize_df(main, neighbor_ids)

        logger.info("Saving outputs to %s", output_paths['output_dir'])
        save_outputs(final_df, audit_df, evaluation_df,
                     neighbor_quality_df, model_selection_df, output_paths)

        # ── execution summary ─────────────────────────────────────────────────
        execution_summary.update({
            'status': 'success',
            'rows_total': int(len(final_df)),
            'quality_summary': quality_summary,
            'missing_report': missing_report,
            'method_counts': main['imputation_method'].value_counts().to_dict(),
            'continuity_flags': continuity_report,
            'reliability': reliability,
        })
        logger.info("Pipeline complete: %s", execution_summary['method_counts'])
        return final_df, evaluation_df, execution_summary

    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        execution_summary.update({'status': 'failed', 'error': str(exc)})
        raise
