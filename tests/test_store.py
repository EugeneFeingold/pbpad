"""Tests for store.py — the single JSON settings file with atomic writes."""
import json
import os

import pytest

from conf import config
import store


def test_get_default_when_missing(temp_settings):
    assert store.get("nope", 42) == 42
    assert store.get("nope") is None


def test_set_persists_and_reloads(temp_settings, monkeypatch):
    store.set("backlight", 7)
    assert temp_settings.exists()
    # Force a cold reload from disk.
    monkeypatch.setattr(store, "_data", None)
    assert store.get("backlight") == 7


def test_none_value_distinct_from_default(temp_settings, monkeypatch):
    store.set("off_timeout", None)          # "Never"
    monkeypatch.setattr(store, "_data", None)
    assert store.get("off_timeout", 60) is None


def test_list_value_roundtrip(temp_settings, monkeypatch):
    store.set("preferred_pbs", ["A", "B"])
    monkeypatch.setattr(store, "_data", None)
    assert store.get("preferred_pbs") == ["A", "B"]


def test_written_file_is_valid_json(temp_settings):
    store.set("x", 1)
    with open(temp_settings) as f:
        assert json.load(f) == {"x": 1}


def test_corrupt_file_treated_as_empty(temp_settings, monkeypatch):
    temp_settings.write_text("}{ not json")
    monkeypatch.setattr(store, "_data", None)
    assert store.get("x", "fallback") == "fallback"


def test_non_dict_json_treated_as_empty(temp_settings, monkeypatch):
    temp_settings.write_text("[1, 2, 3]")
    monkeypatch.setattr(store, "_data", None)
    assert store.get("x", "fallback") == "fallback"


def test_atomic_write_preserves_file_on_crash(temp_settings, monkeypatch):
    # Pre-populate a known-good file.
    temp_settings.write_text(json.dumps({"backlight": 7, "led": 3}))
    monkeypatch.setattr(store, "_data", None)
    store.get("backlight")  # prime cache from disk

    # Make the rename step fail mid-write.
    def boom(a, b):
        raise RuntimeError("simulated kill mid-rename")
    monkeypatch.setattr(os, "replace", boom)

    store.set("backlight", 999)  # should fail internally, not raise

    # Real file must be untouched, and no .tmp left behind.
    with open(temp_settings) as f:
        assert json.load(f) == {"backlight": 7, "led": 3}
    assert not os.path.exists(str(temp_settings) + ".tmp")


def test_save_failure_is_swallowed(temp_settings, monkeypatch):
    # Even a totally broken write path must not propagate.
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("builtins.open", boom)
    store.set("x", 1)  # no raise
