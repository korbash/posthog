from django.test import SimpleTestCase

from posthog.constants import AvailableFeature
from posthog.models import Organization
from posthog.models.activity_logging.retention import get_activity_log_lookback_restriction


class TestActivityLogLookbackRestriction(SimpleTestCase):
    def test_unlimited_entitlement_has_no_lookback_restriction(self) -> None:
        organization = Organization(
            available_product_features=[
                {
                    "key": AvailableFeature.AUDIT_LOGS,
                    "name": "Audit logs",
                    "limit": None,
                    "unit": None,
                }
            ]
        )

        assert get_activity_log_lookback_restriction(organization) is None
