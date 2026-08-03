"""Per-leg operator resolution for JOLT reports.

The "Operator" column (last column of both ``HEADERS`` and ``DIESEL_HEADERS``)
holds a single project operator CODE per leg. The code is resolved from SRF with
a deterministic cascade — **SRF is PRIMARY**, ``vehicles.json`` is only a
fallback / validation layer:

  1. ``leg.trip.trial.description`` round-robin pattern
     ``"JOLT Round Robin: <OP>-<OEM>"`` → captured ``<OP>`` token → code.
     This is the only good operator signal for *shared / round-robin* vehicles
     (their ``vehicle.organisation.name`` is the generic umbrella "JOLT Partners")
     and it tracks operator handovers **per leg / over time**.
  2. ``vehicle.organisation.name`` (static, current-operator) for *dedicated*
     single-operator vehicles whose trial.description is generic — e.g.
     ``"JOLT Nestle-Volvo"`` → ``NESTLE``, ``"William Jackson Food"`` → ``WJF``.
  3. ``vehicles.json`` config fallback (``cfg["operators"]`` time-ranged list or
     ``cfg["operator"]`` single string), if present.
  4. ``None`` (undeterminable).

One company == one operator code (e.g. dedicated YT21EFD = "William Jackson Food"
reuses the existing round-robin token ``WJF``; never mint a parallel code).

The cascade is memoised per report by trip URI (``leg.trip.uri`` is a stored URI
and does NOT trigger an HTTP fetch), so heavily-shared trips resolve once.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ── Curated operator codes (one per company) ─────────────────────────────────
# A code outside this set is treated as "unknown" → triggers a WARN so the next
# uncurated operator is noticed during onboarding.
KNOWN_OPERATOR_CODES = {
    "DP_WORLD",
    "WJF",
    "WELCH_TRANSPORT",
    "JLP",
    "WS",
    "SJG",
    "HTL",
    "PORT_EXPRESS_DAIMLER",
    "KNOWLES",
    "NESTLE",
}

# ── Round-robin trial token (raw, from "JOLT Round Robin: <OP>-<OEM>") → code ─
# Keys are lower-cased raw tokens; values are project codes. Tokens not listed
# here pass through ``_normalize_op_token`` (UPPER_SNAKE) and are flagged unknown.
_TRIAL_OP_TO_CODE = {
    "dp world": "DP_WORLD",
    "wjf": "WJF",
    "welch": "WELCH_TRANSPORT",
    "jlp": "JLP",
    "ws": "WS",
    "sjg": "SJG",
    "port express": "PORT_EXPRESS_DAIMLER",
    "htl": "HTL",
}

# ── Static vehicle / trial organisation.name → code (dedicated vehicles) ──────
_SRF_ORG_TO_CODE = {
    "jolt knowles-volvo": "KNOWLES",
    "jolt nestle-volvo": "NESTLE",
    "jolt jlp-volvo": "JLP",
    "john lewis partnership": "JLP",
    "jolt welch-volvo": "WELCH_TRANSPORT",
    "welch group": "WELCH_TRANSPORT",
    "dp world": "DP_WORLD",
    "william jackson food": "WJF",
}

# Generic umbrella orgs that carry no per-vehicle operator signal.
_GENERIC_SRF_ORGS = {"jolt partners", ""}

# "JOLT Round Robin: <OP>-<OEM>" with OEM ∈ {Scania, DAF, Volvo, Daimler/Mercedes}.
_ROUND_ROBIN_RE = re.compile(
    r"^\s*JOLT\s+Round\s+Robin:\s*(?P<op>.+?)\s*-\s*"
    r"(?:Scania|DAF|Volvo|Daimler|Mercedes)\s*$",
    re.IGNORECASE,
)


def _normalize_op_token(token: str) -> str:
    """UPPER_SNAKE a raw operator token (fallback for uncurated tokens)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", token.strip()).strip("_").upper()


def operator_from_trial_description(desc) -> str | None:
    """Parse a round-robin ``trial.description`` → operator code (or None)."""
    if not desc or not isinstance(desc, str):
        return None
    m = _ROUND_ROBIN_RE.match(desc)
    if not m:
        return None
    raw = m.group("op").strip()
    code = _TRIAL_OP_TO_CODE.get(raw.lower())
    return code if code else _normalize_op_token(raw)


def is_generic_srf_org(org) -> bool:
    """True if ``org`` is the generic umbrella (no per-vehicle operator signal)."""
    return (org or "").strip().lower() in _GENERIC_SRF_ORGS


def normalize_srf_org(org) -> str | None:
    """Map a static ``organisation.name`` → operator code (or None if generic)."""
    if not org or not isinstance(org, str):
        return None
    key = org.strip().lower()
    if key in _GENERIC_SRF_ORGS:
        return None
    return _SRF_ORG_TO_CODE.get(key)


def _leg_trial_description(leg, trial_cache: dict | None) -> str | None:
    """Fetch ``leg.trip.trial.description``, memoised by fetch-free trip URI."""
    trip_uri = None
    try:
        trip_uri = leg.trip.uri  # stored URI, no HTTP fetch
    except Exception:
        trip_uri = None
    if trial_cache is not None and trip_uri is not None and trip_uri in trial_cache:
        return trial_cache[trip_uri]
    desc = None
    try:
        desc = leg.trip.trial.description
    except Exception:
        desc = None
    if trial_cache is not None and trip_uri is not None:
        trial_cache[trip_uri] = desc
    return desc


def _resolve_operator_from_config(reg, leg, vehicles) -> str | None:
    """Fallback to ``vehicles.json`` operator metadata (if present).

    Supports either ``cfg["operators"]`` (a list of time-ranged entries, each
    ``{"code"/"operator": str, "from"/"to": ISO-date-or-null}``) or a single
    ``cfg["operator"]`` string. Returns the code covering ``leg.start_time`` or
    None. The current ``vehicles.json`` carries no operator metadata, so this is
    a no-op for now — kept for forward-compatibility / validation.
    """
    if not vehicles or not reg:
        return None
    cfg = vehicles.get(reg)
    if not cfg:
        return None
    ops = cfg.get("operators")
    if isinstance(ops, list) and ops:
        # Resolve the entry whose [from, to) window contains the leg start.
        leg_start = None
        try:
            import pandas as pd

            leg_start = pd.Timestamp(leg.start_time)
            if leg_start.tzinfo is None:
                leg_start = leg_start.tz_localize("UTC")
        except Exception:
            leg_start = None

        def _ts(val):
            if not val:
                return None
            try:
                import pandas as pd

                t = pd.Timestamp(val)
                return t.tz_localize("UTC") if t.tzinfo is None else t
            except Exception:
                return None

        chosen = None
        for entry in ops:
            code = entry.get("code") or entry.get("operator")
            if not code:
                continue
            if leg_start is None:
                chosen = code  # cannot time-resolve → take first valid
                break
            lo, hi = _ts(entry.get("from")), _ts(entry.get("to"))
            if (lo is None or leg_start >= lo) and (hi is None or leg_start < hi):
                chosen = code
                break
        if chosen:
            return chosen
    single = cfg.get("operator")
    if isinstance(single, str) and single.strip():
        return single.strip()
    return None


def derive_leg_operator(
    leg,
    reg: str | None = None,
    *,
    srf_org_raw: str | None = None,
    vehicles: dict | None = None,
    trial_cache: dict | None = None,
) -> tuple[str | None, str, bool]:
    """Resolve a leg's operator code via the SRF-primary cascade.

    Returns ``(code, source, is_unknown)`` where ``source`` ∈
    {"trial", "srf_org", "config", "none"} and ``is_unknown`` is True when the
    resolved code is not in :data:`KNOWN_OPERATOR_CODES` (or nothing resolved).
    """
    # 1. round-robin trial.description (per-leg, time-varying)
    desc = _leg_trial_description(leg, trial_cache)
    code = operator_from_trial_description(desc)
    if code:
        return code, "trial", code not in KNOWN_OPERATOR_CODES
    # 2. static SRF org (dedicated, fixed-operator vehicles)
    org_code = normalize_srf_org(srf_org_raw)
    if org_code:
        return org_code, "srf_org", org_code not in KNOWN_OPERATOR_CODES
    # 3. vehicles.json config fallback
    cfg_code = _resolve_operator_from_config(reg, leg, vehicles)
    if cfg_code:
        return cfg_code, "config", cfg_code not in KNOWN_OPERATOR_CODES
    # 4. undeterminable
    return None, "none", True
