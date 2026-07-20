"""Tests for log.py — level gating and file output."""
import pytest

import log


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    # Reset module state around each test.
    monkeypatch.setattr(log, "_file", None)
    monkeypatch.setattr(log, "_file_date", "")
    return tmp_path


def test_init_sets_level(logdir):
    log.init(3, log_dir=str(logdir))
    assert log._level == 3


def test_below_threshold_writes_nothing(logdir, capsys):
    log.init(1, log_dir=str(logdir))
    log.log(log.CHANGE, "should not appear")  # CHANGE=2 > 1
    assert capsys.readouterr().out == ""


def test_at_or_above_threshold_prints(logdir, capsys):
    log.init(2, log_dir=str(logdir))
    log.log(log.ERROR, "boom")   # ERROR=1 <= 2
    assert "boom" in capsys.readouterr().out


def test_writes_to_dated_file(logdir):
    log.init(2, log_dir=str(logdir))
    log.log(log.CHANGE, "hello file")
    files = list(logdir.glob("*.log"))
    assert len(files) == 1
    assert "hello file" in files[0].read_text()


def test_level_zero_is_silent(logdir, capsys):
    log.init(0, log_dir=str(logdir))
    log.log(log.ERROR, "nope")
    assert capsys.readouterr().out == ""
    assert list(logdir.glob("*.log")) == []


def test_label_included(logdir, capsys):
    log.init(5, log_dir=str(logdir))
    log.log(log.NETWORK, "msg")
    assert "NETWORK" in capsys.readouterr().out
