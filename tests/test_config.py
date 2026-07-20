"""Sanity checks on config.py constants and their invariants."""
from conf import config


def test_led_groups_match_count():
    # LED_STRIP_GROUPS drives LED_COUNT physical pixels.
    assert len(config.LED_STRIP_GROUPS) >= config.LED_COUNT


def test_led_group_indices_are_lists_of_ints():
    for group in config.LED_STRIP_GROUPS:
        assert isinstance(group, (list, tuple)) and len(group) >= 1
        assert all(isinstance(i, int) and i >= 0 for i in group)


def test_settings_file_is_json_path():
    assert config.SETTINGS_FILE.endswith(".json")


def test_timeout_defaults_sane():
    assert config.DIM_TIMEOUT_DEFAULT > 0
    assert config.OFF_TIMEOUT_DEFAULT >= config.DIM_TIMEOUT_DEFAULT


def test_low_battery_thresholds():
    assert config.LOW_BATTERY_FLOOR_PCT < config.LOW_BATTERY_PCT


def test_led_rate_caps_positive():
    assert config.LED_MAX_FPS > 0
    assert config.PB_PREVIEW_MAX_HZ > 0
    assert config.LED_PLAYBACK_DELAY_SEC > 0


def test_power_pins_distinct():
    pins = {config.POWER_BTN, config.POWER_GATE,
            config.ENC1_A, config.ENC1_B, config.ENC1_SW,
            config.ENC2_A, config.ENC2_B, config.ENC2_SW}
    # All GPIO assignments unique (no accidental pin collisions).
    all_pins = [config.POWER_BTN, config.POWER_GATE,
                config.ENC1_A, config.ENC1_B, config.ENC1_SW,
                config.ENC2_A, config.ENC2_B, config.ENC2_SW]
    assert len(pins) == len(all_pins)
