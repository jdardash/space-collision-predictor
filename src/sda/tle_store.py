"""In-memory TLE catalog."""

from __future__ import annotations

from datetime import datetime, timezone

from sgp4.api import Satrec, WGS72
from sgp4 import exporter

from sda.models import TLERecord


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

    def load_from_text(self, raw: str) -> int:
        """Parse 2-line or 3-line TLE sets from raw text. Returns count ingested."""
        lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
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
                # Extract epoch from the Satrec object
                from sgp4.api import jday
                year = sat.epochyr
                if year < 57:
                    year += 2000
                else:
                    year += 1900
                epoch_jd = sat.jdsatepoch + sat.jdsatepochF
                # Convert JD back to datetime approximately
                from sda.propagator import jd_to_datetime
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
