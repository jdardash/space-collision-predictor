"""Shared physical constants for the SDA collision predictor.

All values use the WGS72 gravity model to match SGP4
(verified against sgp4.propagation.getgravconst("wgs72")).
"""

from __future__ import annotations

# Earth parameters (WGS72)
EARTH_RADIUS_KM = 6378.135  # equatorial radius, matches SGP4 WGS72
EARTH_MU_KM3_S2 = 398600.8  # gravitational parameter, matches SGP4 WGS72

# Default position uncertainty (1-sigma) in km per axis
# 50 m — typical for LEO with fresh TLEs
DEFAULT_POSITION_SIGMA_KM = 0.050

# Default combined hard-body radius (~20 m for two ~10 m objects)
DEFAULT_COMBINED_RADIUS_KM = 0.020
