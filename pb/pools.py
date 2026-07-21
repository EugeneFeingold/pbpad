"""Dedicated thread pools, one per kind of blocking socket work.

The rule: no blocking socket operation ever runs on the shared default executor
(`run_in_executor(None, ...)`). A shared pool couples unrelated tasks through a
fixed thread budget — one hung PixelBlaze `close()` on a dead link once pinned
every default-pool thread and made UDP discovery ("Finding PixelBlaze") hang
until a restart. Isolating each kind of work bounds the blast radius: a stuck
teardown can only starve teardowns, never discovery or polling.

Pools here (stateless / transient work):
  - DISCOVERY : single worker. UDP beacon listen + name/probe reads. Serialized
                on purpose — two listeners can't share the discovery UDP port.
  - TEARDOWN  : closing PixelBlaze WebSockets, which can block on a dead/half-open
                link (bounded by the socket timeout, but a burst of drops
                shouldn't serialize behind one another).

Stateful sockets keep their OWN per-INSTANCE single-worker pool inside
PixelblazeClient / PreviewClient — deliberately not shared and not per-type: on
a reconnect, a fresh instance gets a fresh pool so its connect can't queue
behind the stuck worker of the connection it's replacing.

These are non-daemon pools; the process force-exits via os._exit() on shutdown
(see main.py), so a thread stuck in a blocking close never blocks exit.
"""
from concurrent.futures import ThreadPoolExecutor

DISCOVERY = ThreadPoolExecutor(max_workers=1, thread_name_prefix="discovery")
TEARDOWN = ThreadPoolExecutor(max_workers=4, thread_name_prefix="teardown")
