"""
Logging subsystem.

Levels:
  0  no logging
  1  errors only
  2  connections, pattern changes, slider changes
  3  screen transitions
  4  encoder rotation and presses
  5  PixelBlaze requests and timing
"""
import os
import datetime

INFO       = 0
ERROR      = 1
CHANGE     = 2
TRANSITION = 3
ENCODER    = 4
NETWORK    = 5

_LABELS = {
    INFO:       "INFO      ",
    ERROR:      "ERROR     ",
    CHANGE:     "CHANGE    ",
    TRANSITION: "TRANSITION",
    ENCODER:    "ENCODER   ",
    NETWORK:    "NETWORK   ",
}

_level: int = 0
_log_dir: str = "logs"
_file = None
_file_date: str = ""


def init(level: int, log_dir: str = "logs") -> None:
    global _level, _log_dir
    _level = level
    _log_dir = log_dir
    if level > 0:
        os.makedirs(log_dir, exist_ok=True)


def log(level: int, msg: str) -> None:
    if _level < level:
        return
    global _file, _file_date
    now = datetime.datetime.now()
    ms = now.microsecond // 1000
    ts = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{ms:03d}"
    label = _LABELS.get(level, f"L{level}        ")
    line = f"{ts}  {label}  {msg}"
    print(line, flush=True)
    date_str = now.strftime("%Y%m%d")
    if _file is None or date_str != _file_date:
        if _file is not None:
            _file.close()
        os.makedirs(_log_dir, exist_ok=True)
        _file = open(os.path.join(_log_dir, f"{date_str}.log"), "a")
        _file_date = date_str
    _file.write(line + "\n")
    _file.flush()
