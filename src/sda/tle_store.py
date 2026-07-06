"""In-memory TLE catalog with staleness tracking."""

from __future__ import annotations

from datetime import UTC, datetime

from sgp4.api import WGS72, Satrec

from sda.models import TLEFreshness, TLERecord
from sda.propagator import jd_to_datetime

# Staleness thresholds (hours)
FRESH_THRESHOLD_HOURS = 48.0      # < 2 days
AGING_THRESHOLD_HOURS = 168.0     # < 7 days
STALE_THRESHOLD_HOURS = 336.0     # < 14 days
# > 14 days = EXPIRED


class TLEStore:
    def __init__(self) -> None:
        self._catalog: dict[int, TLERecord] = {}

    def upsert(self, tle: TLERecord) -> None:
        self._catalog[tle.norad_id] = tle

    def get(self, norad_id: int) -> TLERecord | None:
        return self._catalog.get(norad_id)

    def get_all(self) -> list[TLERecord]:
        return list(self._catalog.values())

    def remove(self, norad_id: int) -> bool:
        return self._catalog.pop(norad_id, None) is not None

    def count(self) -> int:
        return len(self._catalog)

    def get_freshness(self, norad_id: int) -> TLEFreshness | None:
        """Check TLE freshness for a satellite."""
        tle = self._catalog.get(norad_id)
        if tle is None:
            return None
        return compute_freshness(tle)

    def get_all_freshness(self) -> list[TLEFreshness]:
        """Get freshness status for all tracked satellites."""
        return [compute_freshness(tle) for tle in self._catalog.values()]

    def get_stale_satellites(
        self, threshold_hours: float = STALE_THRESHOLD_HOURS
    ) -> list[TLEFreshness]:
        """Get satellites with TLEs older than threshold."""
        results = []
        now = datetime.now(UTC)
        for tle in self._catalog.values():
            epoch = tle.epoch.replace(tzinfo=UTC) if tle.epoch.tzinfo is None else tle.epoch
            age_hours = (now - epoch).total_seconds() / 3600.0
            if age_hours > threshold_hours:
                results.append(compute_freshness(tle))
        return results

    def load_from_text(self, raw: str) -> int:
        """Parse 2-line or 3-line TLE sets from raw text. Returns count ingested."""
        lines = [line.strip() for line in raw.strip().splitlines() if line.strip()]
        ingested = 0
        i = 0
        while i < len(lines):
            # Detect if current line is a TLE line 1
            if lines[i].startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
                line1, line2 = lines[i], lines[i + 1]
                name = f"OBJECT {line1[2:7].strip()}"
                i += 2
            elif (
                i + 2 < len(lines)
                and lines[i + 1].startswith("1 ")
                and lines[i + 2].startswith("2 ")
            ):
                name = lines[i]
                line1, line2 = lines[i + 1], lines[i + 2]
                i += 3
            else:
                i += 1
                continue

            try:
                sat = Satrec.twoline2rv(line1, line2, WGS72)
                norad_id = int(line1[2:7].strip())
                # Convert JD epoch to datetime
                epoch = jd_to_datetime(sat.jdsatepoch, sat.jdsatepochF)

                record = TLERecord(
                    norad_id=norad_id,
                    name=name,
                    line1=line1,
                    line2=line2,
                    epoch=epoch,
                )
                self.upsert(record)
                ingested += 1
            except Exception:
                continue

        return ingested


def compute_freshness(tle: TLERecord) -> TLEFreshness:
    """Compute TLE freshness status."""
    now = datetime.now(UTC)
    epoch = tle.epoch.replace(tzinfo=UTC) if tle.epoch.tzinfo is None else tle.epoch
    age_seconds = (now - epoch).total_seconds()
    age_hours = age_seconds / 3600.0
    age_days = age_hours / 24.0

    if age_hours < FRESH_THRESHOLD_HOURS:
        freshness = "FRESH"
        warning = None
    elif age_hours < AGING_THRESHOLD_HOURS:
        freshness = "AGING"
        warning = "TLE is aging; propagation accuracy may be reduced"
    elif age_hours < STALE_THRESHOLD_HOURS:
        freshness = "STALE"
        warning = "TLE is stale; propagation accuracy significantly degraded"
    else:
        freshness = "EXPIRED"
        warning = "TLE is expired; results are unreliable — update immediately"

    return TLEFreshness(
        norad_id=tle.norad_id,
        name=tle.name,
        epoch=tle.epoch,
        age_hours=round(age_hours, 1),
        age_days=round(age_days, 1),
        freshness=freshness,
        accuracy_warning=warning,
    )
