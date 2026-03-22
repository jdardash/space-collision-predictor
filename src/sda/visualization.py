"""3D interactive orbit visualization using Plotly."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import plotly.graph_objects as go

from sda.models import TLERecord, ConjunctionEvent, StateVector
from sda.propagator import propagate_window_numpy
from sda.tle_store import TLEStore


EARTH_RADIUS_KM = 6371.0

# Color palette for orbits
ORBIT_COLORS = [
    "#00d4ff", "#ff6b6b", "#51cf66", "#ffd43b",
    "#cc5de8", "#ff922b", "#20c997", "#e599f7",
]


def create_earth_mesh() -> go.Surface:
    """Create a 3D sphere representing Earth."""
    u = np.linspace(0, 2 * np.pi, 60)
    v = np.linspace(0, np.pi, 30)
    x = EARTH_RADIUS_KM * np.outer(np.cos(u), np.sin(v))
    y = EARTH_RADIUS_KM * np.outer(np.sin(u), np.sin(v))
    z = EARTH_RADIUS_KM * np.outer(np.ones_like(u), np.cos(v))

    return go.Surface(
        x=x, y=y, z=z,
        colorscale=[[0, "#1a3a5c"], [1, "#2d6a9f"]],
        showscale=False,
        opacity=0.7,
        name="Earth",
        hoverinfo="skip",
    )


def plot_orbit_trace(
    positions: np.ndarray,
    name: str,
    color: str,
) -> go.Scatter3d:
    """Create a 3D line trace for an orbit path."""
    return go.Scatter3d(
        x=positions[:, 0],
        y=positions[:, 1],
        z=positions[:, 2],
        mode="lines",
        name=name,
        line=dict(color=color, width=2),
        hovertemplate=f"{name}<br>X: %{{x:.1f}} km<br>Y: %{{y:.1f}} km<br>Z: %{{z:.1f}} km<extra></extra>",
    )


def plot_conjunction_marker(
    event: ConjunctionEvent,
    pos_primary: tuple[float, ...],
    pos_secondary: tuple[float, ...],
) -> list[go.Scatter3d]:
    """Create markers and a miss-distance line at TCA."""
    traces = []

    # Markers at TCA positions
    traces.append(go.Scatter3d(
        x=[pos_primary[0], pos_secondary[0]],
        y=[pos_primary[1], pos_secondary[1]],
        z=[pos_primary[2], pos_secondary[2]],
        mode="markers",
        marker=dict(size=6, color="red", symbol="diamond"),
        name=f"TCA: {event.primary_name} / {event.secondary_name}",
        hovertemplate=(
            f"TCA: {event.tca.strftime('%Y-%m-%d %H:%M:%S')} UTC<br>"
            f"Miss: {event.miss_distance_km:.3f} km<br>"
            f"Risk: {event.risk.value}<extra></extra>"
        ),
    ))

    # Dashed line showing miss distance
    traces.append(go.Scatter3d(
        x=[pos_primary[0], pos_secondary[0]],
        y=[pos_primary[1], pos_secondary[1]],
        z=[pos_primary[2], pos_secondary[2]],
        mode="lines",
        line=dict(color="red", width=3, dash="dash"),
        name=f"Miss: {event.miss_distance_km:.3f} km",
        showlegend=False,
        hoverinfo="skip",
    ))

    return traces


def build_conjunction_figure(
    events: list[ConjunctionEvent],
    store: TLEStore,
    hours: float = 24.0,
    start: datetime | None = None,
) -> go.Figure:
    """Build a complete 3D visualization of orbits and conjunctions."""
    if start is None:
        start = datetime.now(timezone.utc)

    fig = go.Figure()

    # Add Earth
    fig.add_trace(create_earth_mesh())

    # Collect unique satellites involved in events
    sat_ids: set[int] = set()
    for ev in events:
        sat_ids.add(ev.primary)
        sat_ids.add(ev.secondary)

    # If no events, show all tracked satellites
    if not sat_ids:
        sat_ids = {t.norad_id for t in store.get_all()}

    # Propagate and plot each orbit
    orbit_data: dict[int, np.ndarray] = {}
    for i, norad_id in enumerate(sorted(sat_ids)):
        tle = store.get(norad_id)
        if tle is None:
            continue

        positions, _, _ = propagate_window_numpy(tle, start, hours, step_seconds=60.0)
        if len(positions) == 0:
            continue

        orbit_data[norad_id] = positions
        color = ORBIT_COLORS[i % len(ORBIT_COLORS)]
        fig.add_trace(plot_orbit_trace(positions, tle.name, color))

    # Plot conjunction markers
    for event in events:
        tle_a = store.get(event.primary)
        tle_b = store.get(event.secondary)
        if tle_a is None or tle_b is None:
            continue

        from sda.propagator import propagate_at, build_satrec
        try:
            sv_a = propagate_at(build_satrec(tle_a), event.tca)
            sv_b = propagate_at(build_satrec(tle_b), event.tca)
            for trace in plot_conjunction_marker(event, sv_a.position_km, sv_b.position_km):
                fig.add_trace(trace)
        except RuntimeError:
            continue

    # Layout
    max_range = 10000
    fig.update_layout(
        title=dict(
            text="Space-Domain Awareness — Conjunction Visualization",
            font=dict(color="white", size=16),
        ),
        scene=dict(
            xaxis=dict(title="X (km)", color="white", backgroundcolor="#0a0a2e", gridcolor="#1a1a4e"),
            yaxis=dict(title="Y (km)", color="white", backgroundcolor="#0a0a2e", gridcolor="#1a1a4e"),
            zaxis=dict(title="Z (km)", color="white", backgroundcolor="#0a0a2e", gridcolor="#1a1a4e"),
            aspectmode="data",
            bgcolor="#0a0a2e",
        ),
        paper_bgcolor="#0a0a1a",
        plot_bgcolor="#0a0a2e",
        legend=dict(font=dict(color="white")),
        margin=dict(l=0, r=0, t=40, b=0),
    )

    return fig


def render_html(
    events: list[ConjunctionEvent],
    store: TLEStore,
    hours: float = 24.0,
    start: datetime | None = None,
) -> str:
    """Render the conjunction visualization as an HTML string."""
    fig = build_conjunction_figure(events, store, hours, start)
    return fig.to_html(include_plotlyjs="cdn", full_html=True)
