"""Boat polar file loader and (TWA, TWS) → BSP lookup.

CSV format per plan/04 §Boat polars — same layout as OpenCPN / QtVlm:

    TWA\\TWS;4;6;8;10;12;16;20
    0;0;0;0;0;0;0;0
    45;2.1;3.5;4.8;5.5;6.0;6.4;6.8
    ...

- Delimiter auto-detected between `;` (standard) and `,`.
- Bilinear interpolation in (TWA, TWS) space.
- TWA is symmetric: `.bsp(-120, 12) == .bsp(120, 12)`.
- TWS beyond the max column flat-extrapolates (no bonus for more wind).
- TWS below the min column clamps to the min column (below wind floor
  the boat just isn't moving under sail).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


class Polar:
    """Immutable polar table plus interpolation."""

    def __init__(
        self,
        twa_deg: np.ndarray,
        tws_kts: np.ndarray,
        bsp_kts: np.ndarray,
    ) -> None:
        if bsp_kts.shape != (twa_deg.size, tws_kts.size):
            raise ValueError(
                f"bsp grid shape {bsp_kts.shape} does not match "
                f"({twa_deg.size}, {tws_kts.size})"
            )
        self.twa_deg = twa_deg
        self.tws_kts = tws_kts
        self.bsp_kts = bsp_kts

    @classmethod
    def load(cls, path: str | Path) -> Polar:
        text = Path(path).read_text().strip()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        first = lines[0]
        delim = ";" if ";" in first else ","

        header = [c.strip() for c in first.split(delim)]
        # header[0] is "TWA\\TWS" or similar label; skip it.
        tws = np.asarray([float(c) for c in header[1:]], dtype=float)

        twas: list[float] = []
        rows: list[list[float]] = []
        for ln in lines[1:]:
            cells = [c.strip() for c in ln.split(delim)]
            twas.append(float(cells[0]))
            rows.append([float(c) for c in cells[1:]])

        twa = np.asarray(twas, dtype=float)
        grid = np.asarray(rows, dtype=float)
        if not np.all(np.diff(twa) > 0):
            raise ValueError("TWA column must be strictly increasing")
        if not np.all(np.diff(tws) > 0):
            raise ValueError("TWS header must be strictly increasing")
        return cls(twa, tws, grid)

    def bsp(self, twa_deg: float, tws_kts: float) -> float:
        """Boat speed at (TWA, TWS), knots. Always ≥ 0."""
        a = abs(twa_deg) % 360.0
        if a > 180.0:
            a = 360.0 - a

        # Outside the polar's TWA range → boat doesn't sail there.
        if a < self.twa_deg[0] or a > self.twa_deg[-1]:
            return 0.0

        s = max(float(self.tws_kts[0]), min(float(tws_kts), float(self.tws_kts[-1])))

        i = int(np.searchsorted(self.twa_deg, a, side="right") - 1)
        i = min(i, self.twa_deg.size - 2)
        j = int(np.searchsorted(self.tws_kts, s, side="right") - 1)
        j = min(j, self.tws_kts.size - 2)

        a0, a1 = self.twa_deg[i], self.twa_deg[i + 1]
        s0, s1 = self.tws_kts[j], self.tws_kts[j + 1]
        fa = (a - a0) / (a1 - a0) if a1 > a0 else 0.0
        fs = (s - s0) / (s1 - s0) if s1 > s0 else 0.0

        v00 = self.bsp_kts[i, j]
        v01 = self.bsp_kts[i, j + 1]
        v10 = self.bsp_kts[i + 1, j]
        v11 = self.bsp_kts[i + 1, j + 1]
        return float(
            (1 - fa) * (1 - fs) * v00
            + (1 - fa) * fs * v01
            + fa * (1 - fs) * v10
            + fa * fs * v11
        )


DEFAULT_POLAR_PATH = (
    Path(__file__).parent.parent / "data" / "polars" / "cruiser_40ft_moderate.pol"
)
