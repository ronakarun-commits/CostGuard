"""Typed helpers: NumPy ufuncs on a Series return ndarray in the type stubs; these keep Series."""

from __future__ import annotations

import numpy as np
import pandas as pd


def log1p(s: pd.Series) -> pd.Series:
    return pd.Series(np.log1p(s.to_numpy(dtype=float)), index=s.index)


def expm1(s: pd.Series) -> pd.Series:
    return pd.Series(np.expm1(s.to_numpy(dtype=float)), index=s.index)


def dt_index(obj: pd.Series | pd.DataFrame | pd.Index) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(obj if isinstance(obj, pd.Index) else obj.index)
