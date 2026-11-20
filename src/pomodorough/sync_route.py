"""Whether local changes have somewhere to sync to."""

from __future__ import annotations


def sync_route_active(replication_mode: str, *, signed_in: bool) -> bool:
    """The Iroh route is saved only once a usable room exists; the Cloud
    route syncs only with an account. "On device" has no route."""
    if replication_mode == "centralized":
        return signed_in
    return replication_mode == "iroh"
