from frontend.views.tests.recording_state import (
    MIN_CUSTOM_DURATION_MINUTES,
    MIN_RECORDING_SECONDS,
    can_save_recording,
    normalise_max_duration_seconds,
    remaining_minimum_seconds,
)


def test_recording_becomes_valid_at_sixty_seconds():
    assert MIN_RECORDING_SECONDS == 60
    assert not can_save_recording(59.999)
    assert can_save_recording(60)
    assert remaining_minimum_seconds(59) == 1
    assert remaining_minimum_seconds(60) == 0


def test_one_minute_custom_timer_is_valid():
    assert MIN_CUSTOM_DURATION_MINUTES == 1
    assert normalise_max_duration_seconds(59) is None
    assert normalise_max_duration_seconds(60) == 60
