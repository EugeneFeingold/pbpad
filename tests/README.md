# pbpad test suite

Unit tests for the whole app. **No Raspberry Pi hardware required** — all
hardware libraries (`luma`, `gpiozero`, `smbus2`, `spidev`, `pixelblaze`) are
faked in `tests/_fakes.py` and injected into `sys.modules` by the root
`conftest.py` before any project module imports them. PIL stays real, so LCD
rendering is tested against actual pixel output.

## Running

```
pip install -r requirements-dev.txt
pytest
```

Run from the repo root (where `pytest.ini` lives). Useful invocations:

```
pytest                        # everything
pytest tests/test_screens.py  # one module
pytest -k low_battery         # by name
pytest -x -vv                 # stop on first failure, verbose
```

## Layout

| File | Covers |
|------|--------|
| `test_smoke.py`     | every module imports against the fakes |
| `test_store.py`     | JSON settings, atomic writes |
| `test_preferred.py` | preferred-PB ordering |
| `test_log.py`       | log level gating + file output |
| `test_config.py`    | config invariants (pin uniqueness, thresholds) |
| `test_leds.py`      | WS2812 SPI encoding, brightness, blanking |
| `test_battery.py`   | LC709203F CRC + register reads |
| `test_encoders.py`  | rotation filter, press-suppression, switch events |
| `test_power.py`     | power button events, MOSFET wake-gate |
| `test_lcd.py`       | text measurement, render paths, drill/value overlap |
| `test_client.py`    | config parse, control normalize, debounce |
| `test_discovery.py` | device enumeration/probe |
| `test_preview.py`   | preview-frame streaming client |
| `test_wifi.py`      | nmcli/iwlist/proc parsing |
| `test_screens.py`   | velocity stepper, scroller, every Screen |
| `test_main.py`      | App logic: nav stack, low-battery, interpolation |
| `test_performance.py` | render-path cost guards (see below) |
| `test_deploy_parity.py` | deploy.bat / deploy.sh stay equivalent |

## Shared fixtures (in root `conftest.py`)

- `temp_settings` — points `store` at a throwaway file, clears its cache.
- `lcd` — a real `LCD` driving a fake OLED (renders true pixels into PIL).
- `fake_loop` / `fake_queue` — run gpiozero-thread callbacks synchronously.
- `app` (in `test_main.py`) — a fully-constructed `App` with faked hardware.

`_reset_fakes` (autouse) clears all fake-module state between tests.

## Conventions

- **Async tests need no decorator** — `asyncio_mode = auto` in `pytest.ini`.
- Anything that schedules an asyncio task (debounce, preview) must be an
  `async def` test so a running loop exists.
- Tests are **not deployed** — `deploy.bat` ships an explicit file list that
  excludes `tests/`, `conftest.py`, and `pytest.ini`.

## Performance guards

`test_performance.py` protects the OLED render path. A refactor once made every
string measurement render and scan a scratch PIL image, costing ~250% of a Pi
Zero core just to redraw the screen — input went sluggish and the device
hard-froze. Nothing in the suite noticed.

Those tests mostly **count expensive operations** (uncached text measurements,
draw calls) rather than measure wall time, so they're deterministic across
machines. Three wall-clock budgets act as a backstop with 15–60x headroom; they
exist to catch order-of-magnitude regressions, not to police small changes.

**If one fails, don't raise the limit** — find out what started doing real work
per frame. The invariants they encode:

- text measurement and truncation are memoised (each string measured once),
- truncation binary-searches the cut point rather than trimming a character at
  a time,
- a frame whose content hasn't changed performs no drawing at all,
- a frame whose content *has* changed still redraws.

## Maintaining

When you change code, update or add the matching test in the same commit and
run `pytest` before deploying. Add a new `test_*.py` for a new module. Prefer
these tests over one-off `python -c` scripts.
