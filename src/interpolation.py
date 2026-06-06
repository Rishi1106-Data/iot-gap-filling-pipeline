"""interpolation.py — tracking columns + isolated-gap interpolation (notebook Step 7)."""

from typing import Dict, List, Set, Tuple
import pandas as pd
import numpy as np


def init_tracking_columns(
    main: pd.DataFrame,
    ref_col: str,
    gap_type_map: Dict,
    gap_id_map: Dict,
    gap_size_map: Dict,
) -> pd.DataFrame:
    """Initialise the six audit columns on the reindexed grid.

    Columns: gap_type, gap_id, gap_size, imputation_method, confidence_level, filled_flag.
    Present rows start as original/High/0; missing rows start as unresolved/unknown/1.
    """
    main['gap_type'] = main.index.map(lambda t: gap_type_map.get(t, 'original'))
    main['gap_id'] = main.index.map(lambda t: gap_id_map.get(t, 0))
    main['gap_size'] = main.index.map(lambda t: gap_size_map.get(t, 0))
    main['imputation_method'] = np.where(main[ref_col].notna(), 'original', 'unresolved')
    main['confidence_level'] = np.where(main[ref_col].notna(), 'High', 'unknown')
    main['filled_flag'] = np.where(main[ref_col].notna(), 0, 1)
    return main


def fill_isolated_gaps(
    main: pd.DataFrame,
    cont_vars: List[str],
    iso_idx: Set,
    clip: Dict[str, Tuple[float, float]],
    ref_col: str,
) -> pd.DataFrame:
    """Fill isolated (1-row) gaps only, using strict time interpolation (limit=1, inside).

    Medium/long gaps are temporarily masked so interpolation cannot leak into them.
    Filled rows are tagged interpolation / High / filled_flag=1.
    """
    iso_pos = pd.Series(False, index=main.index)
    for ts in iso_idx:
        iso_pos[ts] = True

    for col in cont_vars:
        non_iso = main[col].isna() & ~iso_pos
        main.loc[non_iso, col] = -99999.0
        main[col] = main[col].replace(-99999.0, np.nan).interpolate(
            method='time', limit=1, limit_area='inside')
        main.loc[non_iso & main[col].notna(), col] = np.nan

    iso_filled = iso_pos & main[ref_col].notna()
    main.loc[iso_filled, 'imputation_method'] = 'interpolation'
    main.loc[iso_filled, 'confidence_level'] = 'High'
    main.loc[iso_filled, 'filled_flag'] = 1
    print(f"Isolated gaps filled: {iso_filled.sum():,}")

    return main
