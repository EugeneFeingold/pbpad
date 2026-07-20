"""App internals for pbpad.

main.py's App class is composed from the mixins in this package, split by
responsibility so no single file gets unwieldy:

  - events    input/event loop, navigation stack, screen transitions
  - flows     WiFi setup flows and Settings-value callbacks
  - connection  the WiFi -> discover -> connect -> poll -> recover lifecycle
  - preview   preview stream -> LED strip rendering
  - battery   fuel-gauge polling, low-battery mode, the Info page
  - power     dim / sleep / lock overlays and shutdown / restart
  - util      small stateless helpers (local IP, uptime, fd count)

Every mixin operates on the same App instance; all shared state is created in
App.__init__ (main.py).
"""
