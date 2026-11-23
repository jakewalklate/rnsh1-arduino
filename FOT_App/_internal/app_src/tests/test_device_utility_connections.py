import unittest
from unittest.mock import Mock, patch

from frontend.views.utilities.device_utilities import (
    _leave_utility_page,
    release_device_connections,
)


class DeviceUtilityConnectionTests(unittest.TestCase):
    def test_return_home_keeps_idle_calibration_connection(self):
        flow_service = Mock()
        calibration_service = Mock()
        calibration_service.capture_status.return_value = None

        with (
            patch(
                "frontend.views.utilities.device_utilities.get_flow_calibration_service",
                return_value=flow_service,
            ),
            patch(
                "frontend.views.utilities.device_utilities.get_calibration_service",
                return_value=calibration_service,
            ),
        ):
            _leave_utility_page()

        flow_service.release_device.assert_called_once_with()
        calibration_service.release_device.assert_not_called()

    def test_return_home_releases_an_active_calibration_capture(self):
        flow_service = Mock()
        calibration_service = Mock()
        calibration_service.capture_status.return_value = {"state": "collecting"}

        with (
            patch(
                "frontend.views.utilities.device_utilities.get_flow_calibration_service",
                return_value=flow_service,
            ),
            patch(
                "frontend.views.utilities.device_utilities.get_calibration_service",
                return_value=calibration_service,
            ),
        ):
            _leave_utility_page()

        calibration_service.release_device.assert_called_once_with()

    def test_patient_handoff_releases_all_utility_connections(self):
        flow_service = Mock()
        calibration_service = Mock()

        with (
            patch(
                "frontend.views.utilities.device_utilities.get_flow_calibration_service",
                return_value=flow_service,
            ),
            patch(
                "frontend.views.utilities.device_utilities.get_calibration_service",
                return_value=calibration_service,
            ),
        ):
            release_device_connections()

        flow_service.release_device.assert_called_once_with()
        calibration_service.release_device.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
