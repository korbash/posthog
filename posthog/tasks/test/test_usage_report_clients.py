from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from posthog.tasks.ai_observability_usage_report import get_ph_client as get_ai_observability_ph_client
from posthog.tasks.usage_report import (
    get_ph_client as get_usage_report_ph_client,
    send_report_to_billing_service,
)


class TestUsageReportClients(SimpleTestCase):
    def setUp(self) -> None:
        get_usage_report_ph_client.cache_clear()  # type: ignore[attr-defined]  # cachetools adds this outside its stubs.
        get_ai_observability_ph_client.cache_clear()  # type: ignore[attr-defined]

    def tearDown(self) -> None:
        get_usage_report_ph_client.cache_clear()  # type: ignore[attr-defined]
        get_ai_observability_ph_client.cache_clear()  # type: ignore[attr-defined]

    @override_settings(CLOUD_DEPLOYMENT="US", DEBUG=False, OPT_OUT_CAPTURE=False, TEST=False)
    def test_clients_are_always_disabled(self) -> None:
        usage_client = get_usage_report_ph_client(sync_mode=True)
        ai_observability_client = get_ai_observability_ph_client()

        assert usage_client.disabled
        assert ai_observability_client.disabled

    @override_settings(CLOUD_DEPLOYMENT="US", DEBUG=False, OPT_OUT_CAPTURE=False, TEST=False)
    @patch("posthog.tasks.usage_report.requests.post")
    def test_billing_usage_report_is_always_a_no_op(self, post_mock: MagicMock) -> None:
        send_report_to_billing_service("organization-id", {})

        post_mock.assert_not_called()
