"""3D interactive orbit visualization using Plotly."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import plotly.graph_objects as go

from sda.constants import EARTH_RADIUS_KM
from sda.models import ConjunctionEvent
from sda.propagator import propagate_window_numpy
from sda.tle_store import TLEStore

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
    norad_id: int | None = None,
) -> go.Scatter3d:
    """Create a 3D line trace for an orbit path."""
    n = len(positions)
    altitudes = np.sqrt(np.sum(positions ** 2, axis=1)) - EARTH_RADIUS_KM
    # customdata: [name, norad_id, altitude] for click handler
    cdata = [[name, norad_id or 0, round(float(altitudes[i]), 1)] for i in range(n)]
    return go.Scatter3d(
        x=positions[:, 0],
        y=positions[:, 1],
        z=positions[:, 2],
        mode="lines",
        name=name,
        line=dict(color=color, width=2),
        customdata=cdata,
        hovertemplate=(
            f"{name}<br>"
            "Alt: %{customdata[2]:.1f} km<br>"
            "X: %{x:.1f} km<br>Y: %{y:.1f} km<br>Z: %{z:.1f} km<extra></extra>"
        ),
    )


def plot_screening_volume(
    positions: np.ndarray,
    threshold_km: float,
    name: str,
    color: str,
) -> go.Scatter3d:
    """Create a translucent tube around an orbit path representing the screening volume.

    The screening volume is a cylinder of radius threshold_km centered on the orbit.
    We approximate it by sampling circles perpendicular to the velocity vector.
    """
    # Sample every 10th position to reduce polygon count
    step = max(1, len(positions) // 40)
    sampled = positions[::step]

    if len(sampled) < 3:
        return go.Scatter3d(x=[], y=[], z=[], mode="lines", showlegend=False)

    # Generate tube surface points
    n_circle = 8  # points per cross-section circle
    all_x, all_y, all_z = [], [], []

    for i in range(len(sampled)):
        pos = sampled[i]

        # Velocity direction (forward difference)
        if i < len(sampled) - 1:
            tangent = sampled[i + 1] - sampled[i]
        else:
            tangent = sampled[i] - sampled[i - 1]

        t_norm = np.linalg.norm(tangent)
        if t_norm < 1e-10:
            continue
        tangent = tangent / t_norm

        # Build perpendicular basis
        arb = np.array([1.0, 0.0, 0.0]) if abs(tangent[0]) < 0.9 else np.array([0.0, 1.0, 0.0])

        n1 = np.cross(tangent, arb)
        n1 /= np.linalg.norm(n1)
        n2 = np.cross(tangent, n1)

        # Circle points
        for j in range(n_circle + 1):
            theta = 2 * np.pi * j / n_circle
            point = pos + threshold_km * (np.cos(theta) * n1 + np.sin(theta) * n2)
            all_x.append(point[0])
            all_y.append(point[1])
            all_z.append(point[2])

        # Add None to break the line between circles (creates wireframe effect)
        all_x.append(None)
        all_y.append(None)
        all_z.append(None)

    return go.Scatter3d(
        x=all_x,
        y=all_y,
        z=all_z,
        mode="lines",
        name=f"Screen: {name} ({threshold_km} km)",
        line=dict(color=color, width=1),
        opacity=0.15,
        showlegend=True,
        hoverinfo="skip",
    )


def plot_conjunction_marker(
    event: ConjunctionEvent,
    pos_primary: tuple[float, ...],
    pos_secondary: tuple[float, ...],
) -> list[go.Scatter3d]:
    """Create markers and a miss-distance line at TCA."""
    traces = []

    pc_text = ""
    if event.collision_probability is not None:
        pc_text = f"<br>Pc: {event.collision_probability.probability:.2e}"

    # customdata for click: [type, primary_name, secondary_name, risk, miss_km]
    cdata_row = [
        "conjunction",
        event.primary_name,
        event.secondary_name,
        event.risk.value,
        event.miss_distance_km,
    ]
    conj_cdata = [cdata_row, cdata_row]

    # Markers at TCA positions
    traces.append(go.Scatter3d(
        x=[pos_primary[0], pos_secondary[0]],
        y=[pos_primary[1], pos_secondary[1]],
        z=[pos_primary[2], pos_secondary[2]],
        mode="markers",
        marker=dict(size=6, color="red", symbol="diamond"),
        name=f"TCA: {event.primary_name} / {event.secondary_name}",
        customdata=conj_cdata,
        hovertemplate=(
            f"<b>CONJUNCTION EVENT</b><br>"
            f"TCA: {event.tca.strftime('%Y-%m-%d %H:%M:%S')} UTC<br>"
            f"Miss: {event.miss_distance_km:.3f} km<br>"
            f"Risk: {event.risk.value}{pc_text}<br>"
            "<i>Click for details</i><extra></extra>"
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
    show_screening_volumes: bool = True,
    screening_threshold_km: float = 10.0,
) -> go.Figure:
    """Build a complete 3D visualization of orbits and conjunctions."""
    if start is None:
        start = datetime.now(UTC)

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
        fig.add_trace(plot_orbit_trace(positions, tle.name, color, norad_id))

        # Add screening volume for satellites involved in conjunctions
        involved = {e.primary for e in events} | {e.secondary for e in events}
        if show_screening_volumes and norad_id in involved:
            fig.add_trace(plot_screening_volume(
                positions, screening_threshold_km, tle.name, color
            ))

    # Plot conjunction markers
    for event in events:
        tle_a = store.get(event.primary)
        tle_b = store.get(event.secondary)
        if tle_a is None or tle_b is None:
            continue

        from sda.propagator import build_satrec, propagate_at
        try:
            sv_a = propagate_at(build_satrec(tle_a), event.tca)
            sv_b = propagate_at(build_satrec(tle_b), event.tca)
            for trace in plot_conjunction_marker(event, sv_a.position_km, sv_b.position_km):
                fig.add_trace(trace)
        except RuntimeError:
            continue

    # Layout
    fig.update_layout(
        title=dict(
            text="Space-Domain Awareness — Conjunction Visualization",
            font=dict(color="white", size=16),
        ),
        scene=dict(
            xaxis=dict(
                title="X (km)", color="white", backgroundcolor="#0a0a2e", gridcolor="#1a1a4e"
            ),
            yaxis=dict(
                title="Y (km)", color="white", backgroundcolor="#0a0a2e", gridcolor="#1a1a4e"
            ),
            zaxis=dict(
                title="Z (km)", color="white", backgroundcolor="#0a0a2e", gridcolor="#1a1a4e"
            ),
            aspectmode="data",
            bgcolor="#0a0a2e",
        ),
        paper_bgcolor="#0a0a1a",
        plot_bgcolor="#0a0a2e",
        legend=dict(font=dict(color="white")),
        margin=dict(l=0, r=0, t=40, b=0),
    )

    return fig


_CLICK_HANDLER_JS = """
<script>
(function() {
  // Wait for Plotly to render, then attach click handlers
  var plotEl = document.querySelector('.plotly-graph-div');
  if (!plotEl) {
    // Retry after Plotly finishes rendering
    setTimeout(arguments.callee, 500);
    return;
  }

  plotEl.on('plotly_click', function(data) {
    if (!data || !data.points || data.points.length === 0) return;
    var pt = data.points[0];
    var cd = pt.customdata;
    if (!cd) return;

    if (cd[0] === 'conjunction') {
      // Conjunction marker clicked — notify parent
      window.parent.postMessage({
        action: 'conjClick',
        primary: cd[1],
        secondary: cd[2]
      }, '*');
    } else {
      // Orbit trace clicked — notify parent with satellite name
      window.parent.postMessage({
        action: 'satClick',
        name: cd[0]
      }, '*');
    }
  });

  // Listen for highlight requests from parent dashboard
  window.addEventListener('message', function(e) {
    if (!e.data || e.data.action !== 'highlight') return;
    var name = e.data.name;
    // Find the trace index matching this satellite name and bold it
    var traces = plotEl.data;
    var updates = [];
    for (var i = 0; i < traces.length; i++) {
      if (traces[i].name === name && traces[i].mode === 'lines') {
        Plotly.restyle(plotEl, {'line.width': 5, 'opacity': 1.0}, [i]);
      }
    }
  });
})();
</script>
"""


def render_html(
    events: list[ConjunctionEvent],
    store: TLEStore,
    hours: float = 24.0,
    start: datetime | None = None,
) -> str:
    """Render the conjunction visualization as an HTML string."""
    fig = build_conjunction_figure(events, store, hours, start)
    html: str = fig.to_html(include_plotlyjs="cdn", full_html=True)
    # Inject click handler script before closing </body> tag
    html = html.replace("</body>", _CLICK_HANDLER_JS + "</body>")
    return html
