"""Tests for pb/preferred.py — the most-recent-first preferred-PB list."""
from dataclasses import dataclass

import pytest

from pb import preferred


@dataclass
class Dev:
    name: str
    ip: str = "0.0.0.0"


def test_load_empty(temp_settings):
    assert preferred.load() == []


def test_load_coerces_to_str(temp_settings):
    import store
    store.set("preferred_pbs", [1, 2, "three"])
    assert preferred.load() == ["1", "2", "three"]


def test_load_non_list_is_empty(temp_settings):
    import store
    store.set("preferred_pbs", "notalist")
    assert preferred.load() == []


def test_remember_inserts_at_top(temp_settings):
    preferred.remember("A")
    preferred.remember("B")
    assert preferred.load() == ["B", "A"]


def test_remember_dedupes_and_promotes(temp_settings):
    preferred.remember("A")
    preferred.remember("B")
    preferred.remember("A")  # A already present -> moves to top
    assert preferred.load() == ["A", "B"]


def test_remember_caps_at_max(temp_settings):
    for i in range(preferred._MAX + 5):
        preferred.remember(f"pb{i}")
    names = preferred.load()
    assert len(names) == preferred._MAX
    assert names[0] == f"pb{preferred._MAX + 4}"  # most recent first


def test_pick_returns_highest_ranked_discoverable(temp_settings):
    preferred.remember("B")
    preferred.remember("A")   # order now [A, B]
    devices = [Dev("B"), Dev("C")]  # A not present
    assert preferred.pick(devices).name == "B"


def test_pick_prefers_top_of_list(temp_settings):
    preferred.remember("B")
    preferred.remember("A")   # [A, B]
    devices = [Dev("B"), Dev("A")]
    assert preferred.pick(devices).name == "A"


def test_pick_none_when_no_match(temp_settings):
    preferred.remember("A")
    assert preferred.pick([Dev("X"), Dev("Y")]) is None


def test_pick_none_when_empty(temp_settings):
    assert preferred.pick([Dev("X")]) is None
