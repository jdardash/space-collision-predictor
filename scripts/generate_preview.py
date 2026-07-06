"""Generate a static HTML preview of the 3D orbit visualization.

Run: python scripts/generate_preview.py
Opens the visualization in a browser and saves preview.html.
"""

from datetime import datetime, timezone

from sda.models import TLERecord, ConjunctionEvent, RiskLevel
from sda.tle_store import TLEStore
from sda.visualization import build_conjunction_figure, render_html

# Real TLEs for demo satellites
DEMO_TLES = """ISS (ZARYA)
1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997
2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014
CSS (TIANHE)
1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991
2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001
NOAA 19
1 33591U 09005A   24045.50000000  .00000050  00000+0  40000-4 0  9993
2 33591  99.1900  50.0000 0014000 100.0000 260.0000 14.12300000100003"""


def main():
    store = TLEStore()
    store.load_from_text(DEMO_TLES)

    # Create a sample conjunction event for visualization
    events = [
        ConjunctionEvent(
            primary=25544,
            secondary=48274,
            primary_name="ISS (ZARYA)",
            secondary_name="CSS (TIANHE)",
            tca=datetime(2024, 2, 15, 6, 30, tzinfo=timezone.utc),
            miss_distance_km=3.2,
            relative_velocity_km_s=8.5,
            risk=RiskLevel.MODERATE,
        ),
    ]

    html = render_html(events, store, hours=2.0)

    with open("preview.html", "w") as f:
        f.write(html)
    print("Saved preview.html — open in browser to view")


if __name__ == "__main__":
    main()
