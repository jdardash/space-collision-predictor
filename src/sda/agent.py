"""Typed tool catalogue and agent loop over the SDA orbital engine.

The tools here call this package's Python functions directly. Nothing goes back
out over HTTP to our own API, so the agent works in-process (FastAPI route, MCP
server, or a unit test) with the same code path and the same TLE catalog.

Tool design follows satlas (MIT, ``api/chat.ts`` lines 441-524: six typed tools
with one client-side "highlight the globe" action among them) adapted to this
engine's conjunction and probability functions; the scene-summary tool follows
the rolling HUD summary of gods-eye-view (MIT, ``src/hud.js``), which turns live
camera telemetry into one semantic line for the model to reason over.

Frames and units
----------------
Every state vector is Earth-Centered Inertial (TEME from SGP4) in km and km/s.
Observer coordinates for pass prediction are geodetic latitude and longitude in
degrees, which is a different thing from a state vector and is named as such in
every schema.

Safety
------
The system prompt carries the engine's disclaimer: these are TLE-derived
estimates for situational awareness, not operational collision-avoidance
decisions. Tool errors are captured and handed back to the model as tool
results; they are never raised out to the caller of :func:`run_agent`.
"""

from __future__ import annotations

import importlib
import inspect
import json
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from sda.constants import DEFAULT_COMBINED_RADIUS_KM, EARTH_RADIUS_KM
from sda.llm import ChatResponse, LLMClient, get_client
from sda.models import TLERecord
from sda.tle_store import TLEStore

MAX_ITERATIONS = 6
MAX_TOOL_RESULT_CHARS = 8000

DISCLAIMER = (
    "Results come from public TLEs propagated with SGP4 and are for situational "
    "awareness and engineering study only. They are not an operational "
    "collision-avoidance product and must not be used to make maneuver decisions "
    "for a real spacecraft."
)

SYSTEM_PROMPT = (
    "You are the analyst front end of a space-domain awareness engine that "
    "propagates public two-line element sets with SGP4 and screens them for "
    "conjunctions.\n\n"
    "Rules:\n"
    "1. Answer from tool results, never from memory. If a tool fails, say what "
    "failed and what you would need instead.\n"
    "2. Always state units. Distances are km, speeds km/s, delta-V m/s, angles "
    "degrees, times UTC in ISO 8601. State vectors are Earth-Centered Inertial "
    "(TEME) in km and km/s.\n"
    "3. Collision probability is not the same as risk. Report Pc with the method "
    "that produced it, and mention that a large covariance can lower Pc for a "
    "close approach (probability dilution).\n"
    "4. Risk levels (CRITICAL, HIGH, MODERATE, LOW, NEGLIGIBLE) come from the "
    "engine's miss-distance and relative-velocity thresholds. Do not invent your "
    "own thresholds.\n"
    "5. When the user asks to look at, focus on, or show something, call fly_to "
    "in the same turn as the data tool. fly_to moves the operator's view; it "
    "does not compute anything.\n"
    "6. Be brief. Numbers with units first, caveats second.\n\n"
    "Disclaimer to repeat whenever a maneuver, a reentry or an operational "
    "decision comes up: " + DISCLAIMER
)


# --- Errors ---------------------------------------------------------------

class ToolArgumentError(ValueError):
    """A tool was called with arguments that fail its schema."""


# --- Context --------------------------------------------------------------

def _default_store() -> TLEStore:
    """The running service's catalog when there is one, else an empty store."""
    try:
        from sda.api import store as api_store
    except Exception:
        return TLEStore()
    return api_store


@dataclass
class ToolContext:
    """Everything a tool handler is allowed to touch."""

    store: TLEStore = field(default_factory=_default_store)
    scene: dict[str, Any] | None = None
    now: datetime | None = None

    def time_now(self) -> datetime:
        return self.now or datetime.now(UTC)


# --- Optional cross-package calls ----------------------------------------

def _optional_callable(module_name: str, candidates: Sequence[str]) -> Callable[..., Any] | None:
    """First importable callable among ``candidates`` in ``module_name``.

    Other work packages add modules (pass prediction, extra Pc diagnostics) in
    parallel with this one. Nothing here hard-depends on them: if the module or
    the symbol is missing, the tool degrades and says so.
    """
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return None
    for name in candidates:
        fn = getattr(module, name, None)
        if callable(fn):
            found: Callable[..., Any] = fn
            return found
    return None


def _call_with_supported_kwargs(fn: Callable[..., Any], /, **kwargs: Any) -> Any:
    """Call ``fn`` passing only the keyword arguments its signature accepts."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(**kwargs)
    accepts_any = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
    )
    if accepts_any:
        return fn(**kwargs)
    allowed = {
        name
        for name, p in signature.parameters.items()
        if p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return fn(**{k: v for k, v in kwargs.items() if k in allowed})


# --- Schema validation ----------------------------------------------------

_TYPE_NAMES = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _coerce(name: str, value: Any, spec: dict[str, Any]) -> Any:
    """Coerce one argument to its declared JSON-schema type, or raise."""
    kind = spec.get("type", "string")

    if kind == "integer":
        if isinstance(value, bool):
            raise ToolArgumentError(f"{name}: expected an integer, got a boolean")
        if isinstance(value, int):
            coerced: Any = value
        else:
            try:
                as_float = float(value)
            except (TypeError, ValueError) as exc:
                raise ToolArgumentError(f"{name}: expected an integer, got {value!r}") from exc
            if not float(as_float).is_integer():
                raise ToolArgumentError(f"{name}: expected an integer, got {value!r}")
            coerced = int(as_float)
    elif kind == "number":
        if isinstance(value, bool):
            raise ToolArgumentError(f"{name}: expected a number, got a boolean")
        try:
            coerced = float(value)
        except (TypeError, ValueError) as exc:
            raise ToolArgumentError(f"{name}: expected a number, got {value!r}") from exc
        if not math.isfinite(coerced):
            raise ToolArgumentError(f"{name}: expected a finite number, got {value!r}")
    elif kind == "boolean":
        if isinstance(value, bool):
            coerced = value
        elif isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            coerced = value.strip().lower() == "true"
        else:
            raise ToolArgumentError(f"{name}: expected a boolean, got {value!r}")
    elif kind == "string":
        if not isinstance(value, str):
            raise ToolArgumentError(f"{name}: expected a string, got {value!r}")
        coerced = value
    else:
        expected = _TYPE_NAMES.get(kind, object)
        if not isinstance(value, expected):
            raise ToolArgumentError(f"{name}: expected {kind}, got {type(value).__name__}")
        coerced = value

    choices = spec.get("enum")
    if choices and coerced not in choices:
        raise ToolArgumentError(f"{name}: must be one of {sorted(map(str, choices))}")

    if kind in ("number", "integer"):
        low = spec.get("minimum")
        high = spec.get("maximum")
        if low is not None and coerced < low:
            raise ToolArgumentError(f"{name}: must be >= {low}")
        if high is not None and coerced > high:
            raise ToolArgumentError(f"{name}: must be <= {high}")

    return coerced


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate and coerce tool arguments against a small JSON-schema subset.

    Supports ``type``, ``enum``, ``minimum``, ``maximum``, ``default`` and
    ``required``. Unknown keys are dropped rather than rejected: models
    occasionally invent an extra field and that should not fail a good call.
    """
    properties: dict[str, Any] = schema.get("properties") or {}
    required: list[str] = list(schema.get("required") or [])

    missing = [key for key in required if arguments.get(key) is None]
    if missing:
        raise ToolArgumentError(f"missing required argument(s): {', '.join(sorted(missing))}")

    validated: dict[str, Any] = {}
    for key, spec in properties.items():
        if key in arguments and arguments[key] is not None:
            validated[key] = _coerce(key, arguments[key], spec)
        elif "default" in spec:
            validated[key] = spec["default"]
    return validated


# --- Small shared helpers -------------------------------------------------

def _parse_time(value: Any, fallback: datetime) -> datetime:
    """Parse an ISO 8601 timestamp; naive input is read as UTC."""
    if value in (None, ""):
        return fallback
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolArgumentError(f"could not parse time {value!r} as ISO 8601") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _require_satellite(store: TLEStore, norad_id: int) -> TLERecord:
    tle = store.get(norad_id)
    if tle is None:
        raise ToolArgumentError(f"NORAD {norad_id} is not in the tracked catalog")
    return tle


def _age_hours(tle: TLERecord, at: datetime) -> float:
    epoch = tle.epoch if tle.epoch.tzinfo else tle.epoch.replace(tzinfo=UTC)
    return max(0.0, (at - epoch).total_seconds() / 3600.0)


def _altitude_km(position_km: Sequence[float]) -> float:
    return float(np.linalg.norm(np.asarray(position_km, dtype=float))) - EARTH_RADIUS_KM


def _event_summary(event: Any) -> dict[str, Any]:
    """Flatten a ConjunctionEvent for the model, keeping units in the names."""
    pc = getattr(event, "collision_probability", None)
    return {
        "primary_norad_id": event.primary,
        "primary_name": event.primary_name,
        "secondary_norad_id": event.secondary,
        "secondary_name": event.secondary_name,
        "tca_utc": event.tca.isoformat(),
        "miss_distance_km": round(float(event.miss_distance_km), 4),
        "relative_velocity_km_s": round(float(event.relative_velocity_km_s), 4),
        "risk": str(event.risk),
        "collision_probability": (
            None if pc is None else float(getattr(pc, "probability", 0.0))
        ),
    }


# --- Tool handlers --------------------------------------------------------

def _tool_list_satellites(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    needle = str(args.get("filter") or "").strip().lower()
    limit = int(args.get("limit", 20))
    now = ctx.time_now()

    records = ctx.store.get_all()
    if needle:
        records = [
            t
            for t in records
            if needle in t.name.lower() or needle == str(t.norad_id)
        ]
    records.sort(key=lambda t: t.norad_id)

    return {
        "total_tracked": ctx.store.count(),
        "matched": len(records),
        "returned": min(len(records), limit),
        "satellites": [
            {
                "norad_id": t.norad_id,
                "name": t.name,
                "tle_epoch_utc": t.epoch.isoformat(),
                "tle_age_hours": round(_age_hours(t, now), 2),
            }
            for t in records[:limit]
        ],
    }


def _tool_get_satellite_state(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from sda.propagator import build_satrec, propagate_at

    norad_id = int(args["norad_id"])
    tle = _require_satellite(ctx.store, norad_id)
    when = _parse_time(args.get("time_utc"), ctx.time_now())

    try:
        state = propagate_at(build_satrec(tle), when)
    except RuntimeError as exc:
        raise ToolArgumentError(f"SGP4 propagation failed for {norad_id}: {exc}") from exc

    speed = float(np.linalg.norm(np.asarray(state.velocity_km_s, dtype=float)))
    return {
        "norad_id": norad_id,
        "name": tle.name,
        "epoch_utc": state.epoch.isoformat(),
        "frame": "ECI (TEME)",
        "position_km": [round(v, 4) for v in state.position_km],
        "velocity_km_s": [round(v, 6) for v in state.velocity_km_s],
        "speed_km_s": round(speed, 6),
        "altitude_km": round(_altitude_km(state.position_km), 3),
        "tle_age_hours": round(_age_hours(tle, when), 2),
    }


def _tool_run_conjunction_screen(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from sda.conjunction import find_conjunctions

    hours = float(args.get("hours", 24.0))
    threshold_km = float(args.get("threshold_km", 10.0))
    limit = int(args.get("limit", 10))

    norad_ids: list[int] | None = None
    raw_ids = args.get("norad_ids")
    if isinstance(raw_ids, list) and raw_ids:
        try:
            norad_ids = [int(v) for v in raw_ids]
        except (TypeError, ValueError) as exc:
            raise ToolArgumentError("norad_ids must be a list of integers") from exc

    started = time.monotonic()
    events = _call_with_supported_kwargs(
        find_conjunctions,
        store=ctx.store,
        norad_ids=norad_ids,
        hours=hours,
        threshold_km=threshold_km,
        start=ctx.now,
    )
    elapsed_ms = (time.monotonic() - started) * 1000.0

    counts: dict[str, int] = {}
    for event in events:
        key = str(event.risk)
        counts[key] = counts.get(key, 0) + 1

    return {
        "window_hours": hours,
        "threshold_km": threshold_km,
        "satellites_screened": (
            len(norad_ids) if norad_ids is not None else ctx.store.count()
        ),
        "events_found": len(events),
        "risk_counts": counts,
        "elapsed_ms": round(elapsed_ms, 1),
        "events": [_event_summary(e) for e in events[:limit]],
        "note": DISCLAIMER,
    }


def _pair_at_tca(
    ctx: ToolContext,
    primary_id: int,
    secondary_id: int,
    tca: datetime | None,
) -> tuple[TLERecord, TLERecord, datetime, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return both TLEs and their ECI state vectors at TCA.

    When no TCA is supplied, the pair is screened over the next 24 hours and the
    closest approach found there is used.
    """
    from sda.conjunction import find_conjunctions
    from sda.propagator import build_satrec, propagate_at

    tle_p = _require_satellite(ctx.store, primary_id)
    tle_s = _require_satellite(ctx.store, secondary_id)

    if tca is None:
        events = _call_with_supported_kwargs(
            find_conjunctions,
            store=ctx.store,
            norad_ids=[primary_id, secondary_id],
            hours=24.0,
            threshold_km=200.0,
            start=ctx.now,
            compute_probability=False,
        )
        if not events:
            raise ToolArgumentError(
                f"no close approach within 200 km found for {primary_id} and "
                f"{secondary_id} in the next 24 hours; pass tca_utc explicitly"
            )
        tca = events[0].tca

    try:
        state_p = propagate_at(build_satrec(tle_p), tca)
        state_s = propagate_at(build_satrec(tle_s), tca)
    except RuntimeError as exc:
        raise ToolArgumentError(f"SGP4 propagation failed at TCA: {exc}") from exc

    return (
        tle_p,
        tle_s,
        tca,
        np.asarray(state_p.position_km, dtype=float),
        np.asarray(state_p.velocity_km_s, dtype=float),
        np.asarray(state_s.position_km, dtype=float),
        np.asarray(state_s.velocity_km_s, dtype=float),
    )


def _tool_explain_pc(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from sda.probability import (
        _rotation_matrix_to_conjunction_plane,  # encounter-plane basis, no public alias
        compute_collision_probability_full,
        compute_pc_for_conjunction,
        pc_monte_carlo_2d,
        project_covariance_to_encounter_plane,
        sigma_from_tle_age,
    )

    primary_id = int(args["primary_norad_id"])
    secondary_id = int(args["secondary_norad_id"])
    tca_arg = _parse_time(args.get("tca_utc"), datetime.min.replace(tzinfo=UTC))
    tca_in = None if args.get("tca_utc") in (None, "") else tca_arg
    radius_km = float(args.get("hard_body_radius_km", DEFAULT_COMBINED_RADIUS_KM))

    tle_p, tle_s, tca, pos_p, vel_p, pos_s, vel_s = _pair_at_tca(
        ctx, primary_id, secondary_id, tca_in
    )

    sigma_p = sigma_from_tle_age(_age_hours(tle_p, tca))
    sigma_s = sigma_from_tle_age(_age_hours(tle_s, tca))

    isotropic = compute_pc_for_conjunction(
        pos_primary_km=tuple(pos_p),
        vel_primary_km_s=tuple(vel_p),
        pos_secondary_km=tuple(pos_s),
        vel_secondary_km_s=tuple(vel_s),
        combined_radius_km=radius_km,
        sigma_primary_km=sigma_p,
        sigma_secondary_km=sigma_s,
    )

    cov_p = np.eye(3) * sigma_p**2
    cov_s = np.eye(3) * sigma_s**2
    miss_vector = pos_p - pos_s
    rel_velocity = vel_p - vel_s

    full = compute_collision_probability_full(
        miss_vector_km=miss_vector,
        rel_velocity_km_s=rel_velocity,
        cov_primary_eci_km2=cov_p,
        cov_secondary_eci_km2=cov_s,
        combined_radius_km=radius_km,
    )

    methods: dict[str, float] = {
        "analytic_2d": float(isotropic["probability"]),
        "foster": float(full["probability"]),
        "chan": float(full["probability_chan"]),
    }

    # Monte Carlo in the encounter plane: a fixed seed and a modest sample count
    # so the tool stays interactive and the answer is reproducible.
    n_samples = int(args.get("monte_carlo_samples", 200_000))
    monte_carlo_note: str | None = None
    try:
        cov_2d = project_covariance_to_encounter_plane(cov_p + cov_s, rel_velocity)
        rotation = _rotation_matrix_to_conjunction_plane(rel_velocity)
        miss_2d = (rotation @ miss_vector)[:2]
        methods["monte_carlo_2d"] = float(
            pc_monte_carlo_2d(miss_2d, cov_2d, radius_km, n_samples=n_samples, seed=42)
        )
    except Exception as exc:
        monte_carlo_note = f"Monte Carlo unavailable: {exc}"

    spread = max(methods.values()) - min(methods.values()) if methods else 0.0

    out: dict[str, Any] = {
        "primary_norad_id": primary_id,
        "primary_name": tle_p.name,
        "secondary_norad_id": secondary_id,
        "secondary_name": tle_s.name,
        "tca_utc": tca.isoformat(),
        "miss_distance_km": round(float(np.linalg.norm(miss_vector)), 4),
        "relative_velocity_km_s": round(float(np.linalg.norm(rel_velocity)), 4),
        "hard_body_radius_km": radius_km,
        "sigma_primary_km": round(sigma_p, 6),
        "sigma_secondary_km": round(sigma_s, 6),
        "combined_sigma_km": round(float(isotropic["combined_sigma_km"]), 6),
        "mahalanobis_distance": round(float(full["mahalanobis_distance"]), 4),
        "methods": {k: float(v) for k, v in methods.items()},
        "method_spread": float(spread),
        "monte_carlo_samples": n_samples,
        "interpretation": (
            "Pc is the probability that the two objects come within the combined "
            "hard-body radius at TCA. A stale TLE inflates sigma, which can lower "
            "Pc for a genuinely close approach (probability dilution); risk "
            "classification in this engine ignores Pc for exactly that reason."
        ),
        "note": DISCLAIMER,
    }
    if monte_carlo_note:
        out["monte_carlo_note"] = monte_carlo_note

    # Optional: the Pc usage / applicability checks added by the probability
    # work package. Absent until that lands, and the tool says so.
    usage_fn = _optional_callable(
        "sda.probability",
        ("pc_usage_checks", "pc_usage_check", "usage_checks", "check_pc_usage"),
    )
    if usage_fn is not None:
        try:
            checks = _call_with_supported_kwargs(
                usage_fn,
                miss_vector_km=miss_vector,
                rel_velocity_km_s=rel_velocity,
                cov_primary_eci_km2=cov_p,
                cov_secondary_eci_km2=cov_s,
                cov_combined_eci_km2=cov_p + cov_s,
                cov_combined_km2=cov_p + cov_s,
                combined_radius_km=radius_km,
                hard_body_radius_km=radius_km,
            )
            out["usage_checks"] = _jsonable(checks)
        except Exception as exc:
            out["usage_checks"] = {"error": str(exc)}
    else:
        out["usage_checks"] = "not available in this build"

    return out


def _tool_estimate_decay(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from sda.decay import estimate_decay

    norad_id = int(args["norad_id"])
    tle = _require_satellite(ctx.store, norad_id)

    f107 = args.get("f107_solar_flux")
    kwargs: dict[str, Any] = {"tle": tle}
    if f107 is not None:
        kwargs["f107_solar_flux"] = float(f107)

    result = _call_with_supported_kwargs(estimate_decay, **kwargs)
    payload = result.model_dump(mode="json")
    payload["f107_source"] = "user" if f107 is not None else "engine default"
    payload["note"] = DISCLAIMER
    return dict(payload)


def _tool_plan_maneuver(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from sda.conjunction import classify_risk
    from sda.maneuver import plan_maneuver
    from sda.models import ConjunctionEvent

    primary_id = int(args["primary_norad_id"])
    secondary_id = int(args["secondary_norad_id"])
    target_miss_km = float(args.get("target_miss_km", 5.0))
    tca_in = (
        None
        if args.get("tca_utc") in (None, "")
        else _parse_time(args["tca_utc"], ctx.time_now())
    )

    tle_p, tle_s, tca, pos_p, vel_p, pos_s, vel_s = _pair_at_tca(
        ctx, primary_id, secondary_id, tca_in
    )

    miss_km = float(np.linalg.norm(pos_p - pos_s))
    rel_v_km_s = float(np.linalg.norm(vel_p - vel_s))

    event = ConjunctionEvent(
        primary=primary_id,
        secondary=secondary_id,
        primary_name=tle_p.name,
        secondary_name=tle_s.name,
        tca=tca,
        miss_distance_km=round(miss_km, 4),
        relative_velocity_km_s=round(rel_v_km_s, 4),
        risk=classify_risk(miss_km, rel_v_km_s),
    )

    plan = plan_maneuver(
        event=event,
        tle_primary=tle_p,
        tle_secondary=tle_s,
        target_miss_km=target_miss_km,
    )

    return {
        "primary_norad_id": primary_id,
        "secondary_norad_id": secondary_id,
        "tca_utc": tca.isoformat(),
        "current_miss_km": event.miss_distance_km,
        "relative_velocity_km_s": event.relative_velocity_km_s,
        "risk": str(event.risk),
        "target_miss_km": target_miss_km,
        "warning": plan.warning,
        "recommended": (
            plan.recommended.model_dump(mode="json") if plan.recommended else None
        ),
        "options": [o.model_dump(mode="json") for o in plan.options],
        "delta_v_units": "m/s",
        "note": DISCLAIMER,
    }


def _tool_predict_passes(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    predict = _optional_callable(
        "sda.passes", ("predict_passes", "compute_passes", "find_passes")
    )
    if predict is None:
        raise ToolArgumentError(
            "pass prediction is not available in this build "
            "(sda.passes is not installed)"
        )

    norad_id = int(args["norad_id"])
    tle = _require_satellite(ctx.store, norad_id)
    latitude_deg = float(args["latitude_deg"])
    longitude_deg = float(args["longitude_deg"])
    altitude_km = float(args.get("observer_altitude_m", 0.0)) / 1000.0
    hours = float(args.get("hours", 24.0))
    min_elevation_deg = float(args.get("min_elevation_deg", 10.0))

    # The pass-prediction module owns the observer type. Build one when it
    # exposes it, and offer the flat coordinates too, so this tool keeps
    # working whichever shape that module settles on.
    observer: Any = None
    observer_cls = _optional_callable("sda.passes", ("Observer", "GroundStation", "Site"))
    if observer_cls is not None:
        try:
            observer = observer_cls(
                latitude_deg=latitude_deg,
                longitude_deg=longitude_deg,
                altitude_km=altitude_km,
            )
        except Exception:
            observer = None

    result = _call_with_supported_kwargs(
        predict,
        tle=tle,
        norad_id=norad_id,
        store=ctx.store,
        observer=observer,
        site=observer,
        latitude_deg=latitude_deg,
        longitude_deg=longitude_deg,
        lat_deg=latitude_deg,
        lon_deg=longitude_deg,
        altitude_km=altitude_km,
        altitude_m=altitude_km * 1000.0,
        hours=hours,
        min_elevation_deg=min_elevation_deg,
        start=ctx.time_now(),
    )

    return {
        "norad_id": norad_id,
        "name": tle.name,
        "observer_latitude_deg": latitude_deg,
        "observer_longitude_deg": longitude_deg,
        "window_hours": hours,
        "passes": _jsonable(result),
        "angle_units": "degrees",
        "time_units": "UTC ISO 8601",
    }


def _tool_fly_to(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Client-side view action. Nothing is computed or moved on the server."""
    action: dict[str, Any] = {"type": "fly_to"}

    norad_id = args.get("norad_id")
    if norad_id is not None:
        action["norad_id"] = int(norad_id)
        tle = ctx.store.get(int(norad_id))
        if tle is not None:
            action["name"] = tle.name
    for key in ("latitude_deg", "longitude_deg", "altitude_km"):
        if args.get(key) is not None:
            action[key] = float(args[key])
    label = args.get("label")
    if label:
        action["label"] = str(label)

    if "norad_id" not in action and (
        "latitude_deg" not in action or "longitude_deg" not in action
    ):
        raise ToolArgumentError(
            "fly_to needs either norad_id, or both latitude_deg and longitude_deg"
        )

    return {
        "client_action": action,
        "executed": False,
        "detail": "Queued for the operator's view; the server did not move anything.",
    }


_VIEW_BANDS = (
    (2_000.0, "LEO_CLOSE"),
    (20_000.0, "LEO_REGIONAL"),
    (50_000.0, "MEO"),
    (120_000.0, "GEO_BELT"),
)


def _view_band(altitude_km: float) -> str:
    """Name the camera standoff distance. Adapted from gods-eye-view (MIT)."""
    for ceiling, label in _VIEW_BANDS:
        if altitude_km < ceiling:
            return label
    return "DEEP_SPACE"


def _region_label(latitude_deg: float, longitude_deg: float) -> str:
    """Coarse region name for a geodetic sub-point. Adapted from gods-eye-view."""
    lat, lon = latitude_deg, longitude_deg
    if lat > 72:
        return "ARCTIC"
    if lat < -60:
        return "ANTARCTIC"
    if 5 <= lat <= 83 and -170 <= lon <= -50:
        return "NORTH AMERICA"
    if -60 <= lat <= 15 and -90 <= lon <= -30:
        return "SOUTH AMERICA"
    if 34 <= lat <= 72 and -25 <= lon <= 45:
        return "EUROPE"
    if -35 <= lat <= 38 and -20 <= lon <= 55:
        return "AFRICA"
    if 5 <= lat <= 80 and 45 <= lon <= 180:
        return "ASIA"
    if -50 <= lat <= 5 and 110 <= lon <= 180:
        return "OCEANIA"
    return "NORTHERN OCEANIC GRID" if lat >= 0 else "SOUTHERN OCEANIC GRID"


def _tool_summarize_scene(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Turn the client-supplied scene description into one semantic line.

    Follows the rolling HUD summary of gods-eye-view (MIT): camera band, region,
    what is switched on, and what is selected, compressed into a single string
    the model can reason over without being handed the whole scene graph.
    """
    scene = args.get("scene") if isinstance(args.get("scene"), dict) else ctx.scene
    if not scene:
        return {
            "summary": "No scene was supplied by the client.",
            "camera": None,
            "visible_layers": [],
            "selected": [],
        }

    raw_camera = scene.get("camera")
    camera: dict[str, Any] = raw_camera if isinstance(raw_camera, dict) else {}
    lat = float(camera.get("latitude_deg", 0.0) or 0.0)
    lon = float(camera.get("longitude_deg", 0.0) or 0.0)
    alt = float(camera.get("altitude_km", 0.0) or 0.0)

    raw_layers = scene.get("visible_layers")
    layers = [str(v) for v in raw_layers] if isinstance(raw_layers, list) else []

    raw_selected = scene.get("selected")
    selected: list[dict[str, Any]] = []
    if isinstance(raw_selected, list):
        for item in raw_selected:
            if isinstance(item, dict):
                selected.append(
                    {
                        "norad_id": item.get("norad_id"),
                        "name": item.get("name") or "",
                    }
                )
            else:
                selected.append({"norad_id": None, "name": str(item)})

    when = str(scene.get("time_utc") or ctx.time_now().isoformat())
    tracked = ctx.store.count()

    selected_tag = (
        ", ".join(
            f"{s['name'] or 'object'}"
            + (f" ({s['norad_id']})" if s.get("norad_id") is not None else "")
            for s in selected[:4]
        )
        or "nothing"
    )
    layer_tag = ", ".join(layers[:6]) if layers else "none"

    summary = (
        f"{_view_band(alt)} view over {_region_label(lat, lon)} "
        f"at {lat:.1f} deg lat, {lon:.1f} deg lon, standoff {alt:.0f} km | "
        f"layers: {layer_tag} | selected: {selected_tag} | "
        f"catalog: {tracked} satellites | time {when}"
    )

    return {
        "summary": summary,
        "camera": {
            "latitude_deg": round(lat, 4),
            "longitude_deg": round(lon, 4),
            "altitude_km": round(alt, 2),
            "band": _view_band(alt),
            "region": _region_label(lat, lon),
        },
        "visible_layers": layers,
        "selected": selected,
        "tracked_satellites": tracked,
        "time_utc": when,
    }


# --- Tool catalogue -------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    """One typed tool: JSON schema plus the Python handler behind it."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], dict[str, Any]]
    client_action: bool = False

    def definition(self) -> dict[str, Any]:
        """Provider-independent definition passed to sda.llm."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def _obj(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_satellites",
        description=(
            "List satellites in the tracked TLE catalog, optionally filtered by a "
            "name substring or an exact NORAD id. Use this first when the user "
            "names a satellite so you get its NORAD id before any other tool."
        ),
        parameters=_obj(
            {
                "filter": {
                    "type": "string",
                    "description": (
                        "Case-insensitive substring of the satellite name, or an "
                        "exact NORAD catalog number. Omit to list everything."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum satellites to return. Default 20.",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 20,
                },
            }
        ),
        handler=_tool_list_satellites,
    ),
    ToolSpec(
        name="get_satellite_state",
        description=(
            "Current or future ECI (TEME) state vector for one satellite: "
            "position km, velocity km/s, speed, altitude above the equatorial "
            "radius, and how old the TLE is."
        ),
        parameters=_obj(
            {
                "norad_id": {
                    "type": "integer",
                    "description": "NORAD catalog number, e.g. 25544 for the ISS.",
                    "minimum": 1,
                },
                "time_utc": {
                    "type": "string",
                    "description": (
                        "ISO 8601 UTC timestamp to propagate to. Omit for now."
                    ),
                },
            },
            required=["norad_id"],
        ),
        handler=_tool_get_satellite_state,
    ),
    ToolSpec(
        name="run_conjunction_screen",
        description=(
            "Run the two-phase conjunction screen over the catalog: coarse pair "
            "screening then fine refinement, with risk classification and a "
            "collision probability per event. Use for any question about close "
            "approaches, conjunctions or collision risk."
        ),
        parameters=_obj(
            {
                "threshold_km": {
                    "type": "number",
                    "description": "Miss-distance screening threshold in km. Default 10.",
                    "minimum": 0.001,
                    "maximum": 200.0,
                    "default": 10.0,
                },
                "hours": {
                    "type": "number",
                    "description": "Look-ahead window in hours. Default 24.",
                    "minimum": 0.1,
                    "maximum": 168.0,
                    "default": 24.0,
                },
                "norad_ids": {
                    "type": "array",
                    "description": (
                        "Restrict the screen to these NORAD ids. Omit to screen "
                        "the whole catalog."
                    ),
                    "items": {"type": "integer"},
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum events to return. Default 10.",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 10,
                },
            }
        ),
        handler=_tool_run_conjunction_screen,
    ),
    ToolSpec(
        name="explain_pc",
        description=(
            "Compute collision probability for one satellite pair with every "
            "method the engine has (analytic 2D, Foster quadrature, Chan series, "
            "encounter-plane Monte Carlo) and explain the spread between them. "
            "Use when the user asks how likely a collision is, or why two Pc "
            "numbers disagree."
        ),
        parameters=_obj(
            {
                "primary_norad_id": {
                    "type": "integer",
                    "description": "NORAD id of the primary object.",
                    "minimum": 1,
                },
                "secondary_norad_id": {
                    "type": "integer",
                    "description": "NORAD id of the secondary object.",
                    "minimum": 1,
                },
                "tca_utc": {
                    "type": "string",
                    "description": (
                        "ISO 8601 UTC time of closest approach. Omit to screen the "
                        "pair over the next 24 hours and use the closest approach."
                    ),
                },
                "hard_body_radius_km": {
                    "type": "number",
                    "description": "Combined hard-body radius in km. Default 0.02.",
                    "minimum": 0.0,
                    "maximum": 10.0,
                    "default": DEFAULT_COMBINED_RADIUS_KM,
                },
                "monte_carlo_samples": {
                    "type": "integer",
                    "description": "Encounter-plane Monte Carlo sample count.",
                    "minimum": 1000,
                    "maximum": 2_000_000,
                    "default": 200_000,
                },
            },
            required=["primary_norad_id", "secondary_norad_id"],
        ),
        handler=_tool_explain_pc,
    ),
    ToolSpec(
        name="estimate_decay",
        description=(
            "Estimate atmospheric decay rate, remaining orbital lifetime and "
            "reentry risk for one satellite, scaled by solar activity."
        ),
        parameters=_obj(
            {
                "norad_id": {
                    "type": "integer",
                    "description": "NORAD catalog number.",
                    "minimum": 1,
                },
                "f107_solar_flux": {
                    "type": "number",
                    "description": (
                        "F10.7 solar radio flux in SFU. Omit for the engine "
                        "default (150 = moderate activity)."
                    ),
                    "minimum": 50.0,
                    "maximum": 400.0,
                },
            },
            required=["norad_id"],
        ),
        handler=_tool_estimate_decay,
    ),
    ToolSpec(
        name="plan_maneuver",
        description=(
            "Plan collision-avoidance options for a pair: along-track, "
            "cross-track and radial impulses at several lead times, with the "
            "minimum delta-V option recommended. Delta-V is in m/s."
        ),
        parameters=_obj(
            {
                "primary_norad_id": {
                    "type": "integer",
                    "description": "NORAD id of the maneuvering (primary) object.",
                    "minimum": 1,
                },
                "secondary_norad_id": {
                    "type": "integer",
                    "description": "NORAD id of the non-maneuvering object.",
                    "minimum": 1,
                },
                "tca_utc": {
                    "type": "string",
                    "description": (
                        "ISO 8601 UTC time of closest approach. Omit to find it."
                    ),
                },
                "target_miss_km": {
                    "type": "number",
                    "description": "Desired minimum miss distance in km. Default 5.",
                    "minimum": 0.01,
                    "maximum": 500.0,
                    "default": 5.0,
                },
            },
            required=["primary_norad_id", "secondary_norad_id"],
        ),
        handler=_tool_plan_maneuver,
    ),
    ToolSpec(
        name="predict_passes",
        description=(
            "Predict upcoming passes of a satellite over a ground site: rise, "
            "culmination and set times with azimuth and elevation in degrees. "
            "Requires the pass-prediction module; the tool says so if it is not "
            "installed."
        ),
        parameters=_obj(
            {
                "norad_id": {
                    "type": "integer",
                    "description": "NORAD catalog number.",
                    "minimum": 1,
                },
                "latitude_deg": {
                    "type": "number",
                    "description": (
                        "Observer geodetic latitude in degrees, south negative."
                    ),
                    "minimum": -90.0,
                    "maximum": 90.0,
                },
                "longitude_deg": {
                    "type": "number",
                    "description": (
                        "Observer geodetic longitude in degrees, west negative."
                    ),
                    "minimum": -180.0,
                    "maximum": 180.0,
                },
                "observer_altitude_m": {
                    "type": "number",
                    "description": "Observer height above the ellipsoid in metres.",
                    "minimum": -500.0,
                    "maximum": 9000.0,
                    "default": 0.0,
                },
                "hours": {
                    "type": "number",
                    "description": "Search window in hours. Default 24.",
                    "minimum": 0.1,
                    "maximum": 336.0,
                    "default": 24.0,
                },
                "min_elevation_deg": {
                    "type": "number",
                    "description": "Minimum elevation above the horizon. Default 10.",
                    "minimum": 0.0,
                    "maximum": 89.0,
                    "default": 10.0,
                },
            },
            required=["norad_id", "latitude_deg", "longitude_deg"],
        ),
        handler=_tool_predict_passes,
    ),
    ToolSpec(
        name="fly_to",
        description=(
            "Move the operator's 3D view to a satellite or a point on the globe. "
            "This is a view action carried out by the client, not a computation. "
            "Call it alongside the data tool in the same turn; never wait for the "
            "data tool to finish first."
        ),
        parameters=_obj(
            {
                "norad_id": {
                    "type": "integer",
                    "description": "NORAD id to centre on.",
                    "minimum": 1,
                },
                "latitude_deg": {
                    "type": "number",
                    "description": "Geodetic latitude in degrees, south negative.",
                    "minimum": -90.0,
                    "maximum": 90.0,
                },
                "longitude_deg": {
                    "type": "number",
                    "description": "Geodetic longitude in degrees, west negative.",
                    "minimum": -180.0,
                    "maximum": 180.0,
                },
                "altitude_km": {
                    "type": "number",
                    "description": "Camera standoff distance in km.",
                    "minimum": 0.1,
                    "maximum": 500_000.0,
                },
                "label": {
                    "type": "string",
                    "description": "Short caption for the view, e.g. 'ISS conjunction'.",
                },
            }
        ),
        handler=_tool_fly_to,
        client_action=True,
    ),
    ToolSpec(
        name="summarize_scene",
        description=(
            "Summarize what the operator is currently looking at, from the scene "
            "description the client sent with the request: camera position, "
            "visible layers and selected objects. Use it to resolve 'this one', "
            "'what am I looking at' and similar references."
        ),
        parameters=_obj(
            {
                "scene": {
                    "type": "object",
                    "description": (
                        "Optional scene override with keys camera "
                        "{latitude_deg, longitude_deg, altitude_km}, "
                        "visible_layers (array of strings), selected (array of "
                        "{norad_id, name}) and time_utc. Omit to use the scene "
                        "sent with the request."
                    ),
                }
            }
        ),
        handler=_tool_summarize_scene,
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}


def tool_definitions() -> list[dict[str, Any]]:
    """Canonical tool definitions handed to the LLM provider chain."""
    return [spec.definition() for spec in TOOL_SPECS]


def tool_catalogue() -> list[dict[str, Any]]:
    """Public catalogue for the /agent/tools endpoint and MCP tools/list."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
            "client_action": spec.client_action,
        }
        for spec in TOOL_SPECS
    ]


# --- Execution ------------------------------------------------------------

def _jsonable(value: Any) -> Any:
    """Best-effort conversion of tool output into JSON-serializable data.

    Non-finite floats become None: infinity and NaN are legal in Python but not
    in strict JSON, and a degenerate conjunction (zero relative velocity) puts
    an infinite Mahalanobis distance straight into a tool result.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _jsonable(dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        # Report objects from the probability package (PcUsageReport and
        # friends) are plain dataclasses; keep their fields rather than repr.
        return _jsonable(asdict(value))
    return str(value)


@dataclass
class ToolInvocation:
    """One executed tool call, recorded for the trace returned to the client."""

    name: str
    arguments: dict[str, Any]
    ok: bool
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": _jsonable(self.arguments),
            "ok": self.ok,
            "result": _jsonable(self.result),
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
        }


def execute_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolInvocation:
    """Validate and run one tool. Never raises: failures land in the record."""
    started = time.monotonic()
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        return ToolInvocation(
            name=name,
            arguments=dict(arguments),
            ok=False,
            error=(
                f"unknown tool {name!r}; available tools: "
                + ", ".join(sorted(TOOLS_BY_NAME))
            ),
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    try:
        validated = validate_arguments(spec.parameters, dict(arguments))
        # Array and object members are passed through untouched: the small
        # schema subset validates scalars only.
        for key in ("norad_ids", "scene"):
            if key in arguments and key not in validated:
                validated[key] = arguments[key]
        result = spec.handler(ctx, validated)
    except Exception as exc:
        return ToolInvocation(
            name=name,
            arguments=dict(arguments),
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    return ToolInvocation(
        name=name,
        arguments=validated,
        ok=True,
        result=result,
        duration_ms=(time.monotonic() - started) * 1000.0,
    )


def _tool_result_text(invocation: ToolInvocation) -> str:
    """The string the model sees as the tool result."""
    if invocation.ok:
        payload: Any = _jsonable(invocation.result)
    else:
        payload = {"error": invocation.error}
    text = json.dumps(payload, sort_keys=True, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + '..." (truncated)'
    return text


@dataclass
class AgentResult:
    """Everything one agent turn produced."""

    text: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    trace: list[ToolInvocation] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    provider: str = ""
    model: str = ""
    stop_reason: str = "final"

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "actions": self.actions,
            "trace": [t.to_dict() for t in self.trace],
            "iterations": self.iterations,
            "provider": self.provider,
            "model": self.model,
            "stop_reason": self.stop_reason,
        }


def run_agent(
    messages: Sequence[dict[str, Any]],
    *,
    store: TLEStore | None = None,
    scene: dict[str, Any] | None = None,
    client: LLMClient | None = None,
    max_iterations: int = MAX_ITERATIONS,
    now: datetime | None = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> AgentResult:
    """Run the tool-use loop until the model answers or the cap is reached.

    The loop is bounded by ``max_iterations`` provider round trips. Tool errors
    are fed back to the model as tool results so it can recover or explain; they
    are never raised to the caller. Client-side tools (fly_to) are not executed
    on the server: their action payload is collected into ``actions``.
    """
    llm = client or get_client()
    ctx = ToolContext(
        store=store if store is not None else _default_store(),
        scene=scene,
        now=now,
    )

    convo: list[dict[str, Any]] = [dict(m) for m in messages]
    actions: list[dict[str, Any]] = []
    trace: list[ToolInvocation] = []
    definitions = tool_definitions()

    response = ChatResponse(text="")
    iterations = 0
    for _ in range(max(1, max_iterations)):
        iterations += 1
        response = llm.chat(convo, tools=definitions, system=system_prompt)

        if not response.tool_calls:
            return AgentResult(
                text=response.text,
                actions=actions,
                trace=trace,
                messages=convo,
                iterations=iterations,
                provider=response.provider,
                model=response.model,
                stop_reason="final",
            )

        convo.append(
            {
                "role": "assistant",
                "content": response.text,
                "tool_calls": [c.to_dict() for c in response.tool_calls],
            }
        )

        for call in response.tool_calls:
            invocation = execute_tool(call.name, call.arguments, ctx)
            trace.append(invocation)
            if invocation.ok:
                spec = TOOLS_BY_NAME.get(call.name)
                if spec is not None and spec.client_action:
                    action = invocation.result.get("client_action") if invocation.result else None
                    if isinstance(action, dict):
                        actions.append(action)
            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": _tool_result_text(invocation),
                }
            )

    return AgentResult(
        text=response.text
        or (
            "Reached the tool-call limit before finishing. The tool trace holds "
            "everything that was computed."
        ),
        actions=actions,
        trace=trace,
        messages=convo,
        iterations=iterations,
        provider=response.provider,
        model=response.model,
        stop_reason="max_iterations",
    )


__all__ = [
    "DISCLAIMER",
    "MAX_ITERATIONS",
    "SYSTEM_PROMPT",
    "TOOLS_BY_NAME",
    "TOOL_SPECS",
    "AgentResult",
    "ToolArgumentError",
    "ToolContext",
    "ToolInvocation",
    "ToolSpec",
    "execute_tool",
    "run_agent",
    "tool_catalogue",
    "tool_definitions",
    "validate_arguments",
]
