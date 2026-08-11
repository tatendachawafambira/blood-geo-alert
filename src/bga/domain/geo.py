"""In-Python distance helper, for display only.

Nothing in the matching path uses this. Every distance the system acts on is
computed by PostGIS on the spheroid and stored in
core.ward_collection_proximity (ADR-011). This function exists so a template
can print "about 2 km away" without a database round trip.

Keeping the two apart is deliberate. If a caller ever needs an authoritative
distance, the right move is to read the precomputed metres, not to call this.
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

# IUGG mean earth radius in metres. PostGIS geography uses the WGS-84
# spheroid rather than a sphere, so haversine and ST_Distance disagree by a
# few tenths of a percent. That is well inside the precision this system
# claims: ward-centroid anchoring means distances are approximate by design
# (SAD 7.3.3), and the interface must not present them as exact.
EARTH_RADIUS_METRES = 6371008.8


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS-84 points, in metres."""
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = radians(lon2 - lon1)

    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_METRES * asin(sqrt(a))
