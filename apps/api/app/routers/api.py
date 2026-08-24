"""Deprecated shim.

The monolithic router was split into per-domain modules; ``app.routers`` now
exposes the aggregated router. Kept so ``from app.routers.api import router``
keeps working for anything still importing the old path.
"""

from __future__ import annotations

from app.routers import router

__all__ = ["router"]
