"""One JSON file for all persisted settings — backlight, idle timeouts, the
preferred-PixelBlaze list, etc. (WiFi credentials live in NetworkManager, not
here.) Loaded once, written on every change.

Writes are atomic (temp file + rename): if the process is killed mid-write,
the real settings file is untouched and next boot loads the previous version.
Truncating in place would risk leaving an empty or partial JSON file that
fails to parse and silently resets every setting to defaults."""
import json
import os

from conf import config
import log

_data = None


def _load() -> dict:
    global _data
    if _data is None:
        try:
            with open(config.SETTINGS_FILE) as f:
                loaded = json.load(f)
            _data = loaded if isinstance(loaded, dict) else {}
        except Exception:
            _data = {}
    return _data


def get(key, default=None):
    return _load().get(key, default)


def set(key, value):
    _load()[key] = value
    tmp = config.SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_data, f, indent=2)
        os.replace(tmp, config.SETTINGS_FILE)   # atomic rename on POSIX
    except Exception as e:
        log.log(log.ERROR, f"settings save failed: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
