# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | Yes       |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do not** open a public GitHub issue
2. Email **jsdardashti@gmail.com** with:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact assessment

This is a maintainer-run project; reports are typically acknowledged within a week.

## Safety-Critical Components

This project contains safety-critical code paths where bugs could lead to incorrect risk assessments:

| Component | File | Concern |
|-----------|------|---------|
| Risk classification | `src/sda/conjunction.py` | Incorrect threat levels could cause missed collision warnings |
| Collision probability | `src/sda/probability.py` | Numerical errors could under/overestimate Pc |
| SGP4 propagation | `src/sda/propagator.py` | Wrong coordinate frame or gravity model produces invalid orbits |
| Maneuver planning | `src/sda/maneuver.py` | Incorrect delta-V could worsen conjunction geometry |

Changes to these components undergo mandatory safety review before merge.

## Scope

This tool is for **research and educational purposes**. It should not be used as a sole source of truth for operational satellite collision avoidance decisions.
