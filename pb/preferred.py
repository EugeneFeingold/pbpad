"""Persisted preferred-PixelBlaze list, most-recent-first.

Works like known WiFi networks: the manager auto-connects to the highest-ranked
PixelBlaze that's currently discoverable. Connecting to (or manually selecting)
one moves it to the top. Keyed by device name, since the IP is DHCP-assigned and
the discovery device_id is only hash(ip) — neither is stable.
"""
import store

_KEY = "preferred_pbs"
_MAX = 20


def load() -> list:
    data = store.get(_KEY, [])
    return [str(x) for x in data] if isinstance(data, list) else []


def remember(name: str):
    """Move `name` to the top of the preferred list and persist."""
    names = [n for n in load() if n != name]
    names.insert(0, name)
    store.set(_KEY, names[:_MAX])


def pick(devices):
    """Return the discoverable device highest in the preferred list, or None."""
    by_name = {d.name: d for d in devices}
    for name in load():
        if name in by_name:
            return by_name[name]
    return None
