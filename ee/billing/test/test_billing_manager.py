import datetime
from typing import Any, cast

from posthog.test.base import BaseTest
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

import jwt
from parameterized import parameterized
from rest_framework.exceptions import NotAuthenticated

from posthog.models.organization import Organization, OrganizationMembership
from posthog.models.user import User

from ee.billing.billing_manager import (
    BillingManager,
    FundingStatusUnavailable,
    OrganizationFundingStatus,
    PrepaidCreditState,
    _get_user_organization_role,
    _parse_funding_status,
    build_billing_token,
)
from ee.billing.billing_types import BillingProvider, BillingStatus, Product
from ee.models.license import License, LicenseManager


def create_default_products_response(**kwargs) -> dict[str, list[Product]]:
    data: Any = {
        "products": [
            Product(
                name="Product analytics",
                headline="Product analytics with autocapture",
                description="A comprehensive product analytics platform built to natively work with session replay, feature flags, experiments, and surveys.",
                usage_key="events",
                image_url="https://posthog.com/images/products/product-analytics/product-analytics.png",
                docs_url="https://posthog.com/docs/product-analytics",
                type="product_analytics",
                unit="event",
                contact_support=False,
                inclusion_only=False,
                icon_key="IconGraph",
                plans=[],
                addons=[],
            )
        ]
    }

    data.update(kwargs)
    return data


class TestFundingStatusParsing(SimpleTestCase):
    @parameterized.expand(
        [
            (
                "normal",
                {"startup_program_label": None, "prepaid_credit_state": "none"},
                OrganizationFundingStatus(
                    startup_program_label=None,
                    prepaid_credit_state=PrepaidCreditState.NONE,
                ),
            ),
            (
                "startup_active",
                {"startup_program_label": "Startup", "prepaid_credit_state": "active"},
                OrganizationFundingStatus(
                    startup_program_label="Startup",
                    prepaid_credit_state=PrepaidCreditState.ACTIVE,
                ),
            ),
            (
                "yc_expired",
                {"startup_program_label": "YC", "prepaid_credit_state": "expired"},
                OrganizationFundingStatus(
                    startup_program_label="YC",
                    prepaid_credit_state=PrepaidCreditState.EXPIRED,
                ),
            ),
        ]
    )
    def test_parses_funding_status(
        self, _name: str, payload: dict[str, str | None], expected: OrganizationFundingStatus
    ) -> None:
        self.assertEqual(_parse_funding_status(payload), expected)

    @parameterized.expand(
        [
            ("not_an_object", []),
            (
                "invalid_program_label",
                {"startup_program_label": "Growth", "prepaid_credit_state": "none"},
            ),
            ("missing_program_label", {"prepaid_credit_state": "none"}),
            ("missing_credit_state", {"startup_program_label": None}),
            (
                "invalid_credit_state",
                {"startup_program_label": None, "prepaid_credit_state": "paid"},
            ),
        ]
    )
    def test_rejects_invalid_funding_status(self, _name: str, payload: object) -> None:
        with self.assertRaises(FundingStatusUnavailable):
            _parse_funding_status(payload)


class TestBillingManager(SimpleTestCase):
    def setUp(self) -> None:
        self.organization = Organization(
            name="Local organization",
            available_product_features=[{"key": "alerts", "name": "Alerts", "limit": None}],
        )
        self.manager = BillingManager(license=None)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected billing HTTP"))
        network.start()
        self.addCleanup(network.stop)

    @parameterized.expand(
        (
            ("activate_subscription", ({},), {"success": True}),
            ("deactivate_products", ("all",), None),
            ("update_billing", ({"custom_limits_usd": {"events": 0}},), None),
            ("update_billing_organization_users", (), None),
            ("activate_trial", ({},), {"success": True}),
            ("cancel_trial", ({},), None),
            ("switch_plan", ({},), {"success": True}),
            ("purchase_credits", ({"annual_credit_amount_usd": 100},), {"success": True}),
            ("authorize", (), {"success": True, "status": "success", "clientSecret": None}),
            ("authorize_status", ({},), {"success": True, "status": "success"}),
            ("deauthorize", (BillingProvider.VERCEL,), {"success": True}),
            ("apply_startup_program", ({},), {"success": True}),
            ("claim_coupon", ({"code": "example"},), {"success": True}),
            ("coupons_overview", (), {"claimed_coupons": []}),
            (
                "dispute_signals_pr",
                ({"refund_id": "example", "credits": 100},),
                {"success": True, "credit_amount_usd": "0", "zero_reason": "self_hosted"},
            ),
            ("get_invoices", (), {"count": 0, "results": [], "portal_url": "/organization/billing"}),
        )
    )
    def test_local_operations_preserve_features_without_network(
        self, method: str, args: tuple[object, ...], expected: object
    ) -> None:
        assert self.organization.available_product_features is not None
        features = list(self.organization.available_product_features)
        for license in (None, License(key="example::secret", plan="enterprise")):
            self.manager.license = license
            self.assertEqual(getattr(self.manager, method)(self.organization, *args), expected)
            self.assertEqual(self.organization.available_product_features, features)

    @parameterized.expand([("get_usage_data",), ("get_spend_data",)])
    def test_local_timeseries_has_empty_results(self, method: str) -> None:
        response = getattr(self.manager, method)(self.organization, {"team_ids": [1]})
        self.assertEqual(
            response,
            {
                "status": "ok",
                "type": "timeseries",
                "customer_id": "",
                "results": [],
                "team_id_options": [],
            },
        )

    def test_billing_reads_stored_features_and_reports_zero_spend(self) -> None:
        response = self.manager.get_billing(self.organization)
        self.assertEqual(response["available_product_features"], self.organization.available_product_features)
        self.assertEqual(response["products"], [])
        self.assertEqual(response["current_total_amount_usd_after_discount"], "0")
        self.assertEqual(
            self.manager.get_billing_status_for_alerts(self.organization)["customer"],
            {key: value for key, value in response.items() if key != "stripe_portal_url"},
        )
        self.organization.available_product_features = []
        self.assertEqual(self.manager.get_billing(self.organization)["available_product_features"], [])
        self.assertEqual(self.manager.get_billing(None)["available_product_features"], [])

    def test_local_funding_and_webhook_do_not_require_credentials(self) -> None:
        self.assertEqual(
            self.manager.get_funding_status(self.organization),
            OrganizationFundingStatus(startup_program_label=None, prepaid_credit_state=PrepaidCreditState.NONE),
        )
        self.assertEqual(self.manager.credits_overview(self.organization)["credit_brackets"], [])
        self.manager.handle_billing_provider_webhook("invoice.created", {}, self.organization, "vercel")

    def test_incoming_billing_data_cannot_downgrade_local_features(self) -> None:
        assert self.organization.available_product_features is not None
        features = list(self.organization.available_product_features)
        self.manager.update_org_details(
            self.organization, cast(BillingStatus, {"customer": {"available_product_features": []}})
        )
        self.assertEqual(self.organization.available_product_features, features)


class TestBuildBillingToken(BaseTest):
    def setUp(self):
        super().setUp()
        self.license = super(LicenseManager, cast(LicenseManager, License.objects)).create(
            key="license_id::license_secret",
            plan="enterprise",
            valid_until=datetime.datetime(2038, 1, 19, 3, 14, 7),
        )

    def test_build_billing_token_without_user(self):
        """Token without user should have basic organization info only"""
        token = build_billing_token(self.license, self.organization)

        decoded = jwt.decode(token, "license_secret", algorithms=["HS256"], audience="posthog:license-key")

        assert decoded["id"] == "license_id"
        assert decoded["organization_id"] == str(self.organization.id)
        assert decoded["organization_name"] == self.organization.name
        assert decoded["aud"] == "posthog:license-key"
        assert "distinct_id" not in decoded
        assert "organization_role" not in decoded
        assert "original_role" not in decoded
        # Only service-to-service tokens carry service_action; billing uses its absence
        # to reject user-minted tokens on service-only endpoints.
        assert "service_action" not in decoded

    def test_build_billing_token_with_service_action(self):
        token = build_billing_token(self.license, self.organization, service_action="signals_pr_dispute")

        decoded = jwt.decode(token, "license_secret", algorithms=["HS256"], audience="posthog:license-key")

        assert decoded["service_action"] == "signals_pr_dispute"
        assert "distinct_id" not in decoded
        assert "organization_role" not in decoded

    def test_build_billing_token_with_user_who_is_member(self):
        """Token with user should include distinct_id and organization_role as level display string"""
        token = build_billing_token(self.license, self.organization, user=self.user)

        decoded = jwt.decode(token, "license_secret", algorithms=["HS256"], audience="posthog:license-key")

        assert decoded["id"] == "license_id"
        assert decoded["organization_id"] == str(self.organization.id)
        assert decoded["distinct_id"] == str(self.user.distinct_id)
        # organization_role should be a level display string (e.g., "member", "administrator", "owner")
        assert decoded["organization_role"] in ["member", "administrator", "owner"]
        assert "original_role" not in decoded

    def test_build_billing_token_raises_when_no_organization(self):
        """Should raise NotAuthenticated when organization is None"""
        with self.assertRaises(NotAuthenticated):
            build_billing_token(self.license, None)

    def test_build_billing_token_raises_when_no_license(self):
        """Should raise NotAuthenticated when license is None"""
        with self.assertRaises(NotAuthenticated):
            build_billing_token(None, self.organization)

    def test_build_billing_token_raises_when_user_not_in_organization(self):
        """Should raise NotAuthenticated when user (acting as authorizer) is not a member of the organization"""
        other_org = Organization.objects.create(name="Other Org")
        non_member_user = User.objects.create_and_join(
            organization=other_org,
            email="nonmember@example.com",
            password=None,
        )

        with self.assertRaises(NotAuthenticated) as ctx:
            build_billing_token(self.license, self.organization, user=non_member_user)

        # When user is provided without authorizer_actor, user becomes the authorizer
        assert "Authorizer is not part of organization" in str(ctx.exception.detail)

    @patch("posthog.event_usage.posthoganalytics.capture")
    def test_build_billing_token_with_authorizer_actor_same_as_user(self, mock_capture):
        """When authorizer_actor equals user, no privilege escalation occurs"""
        token = build_billing_token(self.license, self.organization, user=self.user, authorizer_actor=self.user)

        decoded = jwt.decode(token, "license_secret", algorithms=["HS256"], audience="posthog:license-key")

        assert decoded["distinct_id"] == str(self.user.distinct_id)
        assert decoded["organization_role"] in ["member", "administrator", "owner"]
        assert "original_role" not in decoded
        mock_capture.assert_not_called()

    @patch("posthog.event_usage.posthoganalytics.capture")
    def test_build_billing_token_with_privilege_escalation(self, mock_capture):
        """When authorizer_actor differs from user, original_role is set and capture is called"""
        member_user = User.objects.create_and_join(
            organization=self.organization,
            email="member@example.com",
            password=None,
            level=OrganizationMembership.Level.MEMBER,
        )
        admin_authorizer = User.objects.create_and_join(
            organization=self.organization,
            email="admin@example.com",
            password=None,
            level=OrganizationMembership.Level.ADMIN,
        )

        token = build_billing_token(
            self.license, self.organization, user=member_user, authorizer_actor=admin_authorizer
        )

        decoded = jwt.decode(token, "license_secret", algorithms=["HS256"], audience="posthog:license-key")

        assert decoded["distinct_id"] == str(member_user.distinct_id)
        # organization_role should be the authorizer's role (administrator)
        assert decoded["organization_role"] == "administrator"
        # original_role should be the user's actual role (member)
        assert decoded["original_role"] == "member"

        mock_capture.assert_called_once()
        call_kwargs = mock_capture.call_args[1]
        assert call_kwargs["event"] == "$billing_privilege_escalation"
        assert call_kwargs["distinct_id"] == str(admin_authorizer.distinct_id)
        assert call_kwargs["properties"]["target_user_id"] == member_user.id
        assert call_kwargs["properties"]["target_distinct_id"] == str(member_user.distinct_id)
        assert call_kwargs["properties"]["target_email"] == member_user.email
        assert call_kwargs["properties"]["action"] == "update_billing"

    def test_build_billing_token_raises_when_authorizer_actor_not_in_organization(self):
        """Should raise NotAuthenticated when authorizer_actor is not a member of the organization"""
        other_org = Organization.objects.create(name="Other Org")
        non_member_authorizer = User.objects.create_and_join(
            organization=other_org,
            email="nonmember_authorizer@example.com",
            password=None,
        )

        with self.assertRaises(NotAuthenticated) as ctx:
            build_billing_token(self.license, self.organization, user=self.user, authorizer_actor=non_member_authorizer)

        assert "Authorizer is not part of organization" in str(ctx.exception.detail)

    @patch("posthog.event_usage.posthoganalytics.capture")
    def test_build_billing_token_privilege_escalation_user_not_member_allowed(self, mock_capture):
        """When authorizer_actor is valid but user is not a member, original_role should be None"""
        other_org = Organization.objects.create(name="Other Org")
        non_member_user = User.objects.create_and_join(
            organization=other_org,
            email="nonmember@example.com",
            password=None,
        )
        valid_authorizer = User.objects.create_and_join(
            organization=self.organization,
            email="valid_authorizer@example.com",
            password=None,
            level=OrganizationMembership.Level.ADMIN,
        )

        token = build_billing_token(
            self.license, self.organization, user=non_member_user, authorizer_actor=valid_authorizer
        )

        decoded = jwt.decode(token, "license_secret", algorithms=["HS256"], audience="posthog:license-key")

        # Token should have non-member user's distinct_id
        assert decoded["distinct_id"] == str(non_member_user.distinct_id)
        # organization_role should be the authorizer's role
        assert decoded["organization_role"] == "administrator"
        # original_role should be None since user is not a member
        assert decoded["original_role"] is None

        # Privilege escalation capture should still be called
        mock_capture.assert_called_once()
        call_kwargs = mock_capture.call_args[1]
        assert call_kwargs["event"] == "$billing_privilege_escalation"
        assert call_kwargs["distinct_id"] == str(valid_authorizer.distinct_id)
        assert call_kwargs["properties"]["target_user_id"] == non_member_user.id
        assert call_kwargs["properties"]["target_distinct_id"] == str(non_member_user.distinct_id)
        assert call_kwargs["properties"]["target_email"] == non_member_user.email

    @parameterized.expand(
        [
            (OrganizationMembership.Level.MEMBER, "member"),
            (OrganizationMembership.Level.ADMIN, "administrator"),
            (OrganizationMembership.Level.OWNER, "owner"),
        ]
    )
    def test_build_billing_token_user_role_populated_for_all_levels(self, level, expected_role_display):
        """organization_role should be the correct level display string for each membership level"""
        user_with_level = User.objects.create_and_join(
            organization=self.organization,
            email=f"user_level_{level}@example.com",
            password=None,
            level=level,
        )

        token = build_billing_token(self.license, self.organization, user=user_with_level)

        decoded = jwt.decode(token, "license_secret", algorithms=["HS256"], audience="posthog:license-key")

        assert decoded["organization_role"] == expected_role_display

    def test_build_billing_token_without_user_but_with_authorizer_actor(self):
        """When user is None but authorizer_actor is provided, authorizer_actor should be ignored"""
        admin_user = User.objects.create_and_join(
            organization=self.organization,
            email="admin@example.com",
            password=None,
            level=OrganizationMembership.Level.ADMIN,
        )

        token = build_billing_token(self.license, self.organization, user=None, authorizer_actor=admin_user)

        decoded = jwt.decode(token, "license_secret", algorithms=["HS256"], audience="posthog:license-key")

        # Without user, no user-related fields should be in the token
        assert "distinct_id" not in decoded
        assert "organization_role" not in decoded
        assert "original_role" not in decoded


class TestGetUserOrganizationRole(BaseTest):
    def test_returns_role_display_for_valid_member(self):
        """Should return the role display string for a user who is a member"""
        role_display = _get_user_organization_role(self.user, self.organization)
        # Should be a valid role display string
        assert role_display in ["member", "administrator", "owner"]

    @parameterized.expand(
        [
            (OrganizationMembership.Level.MEMBER, "member"),
            (OrganizationMembership.Level.ADMIN, "administrator"),
            (OrganizationMembership.Level.OWNER, "owner"),
        ]
    )
    def test_returns_correct_role_display_for_each_level(self, level, expected_display):
        """Should return the correct display string for each membership level"""
        user_with_level = User.objects.create_and_join(
            organization=self.organization,
            email=f"user_level_{level}_helper@example.com",
            password=None,
            level=level,
        )
        role_display = _get_user_organization_role(user_with_level, self.organization)
        assert role_display == expected_display

    def test_returns_none_for_non_member(self):
        """Should return None for a user who is not a member"""
        other_org = Organization.objects.create(name="Other Org")
        non_member = User.objects.create_and_join(
            organization=other_org,
            email="nonmember@example.com",
            password=None,
        )

        role_display = _get_user_organization_role(non_member, self.organization)
        assert role_display is None


class TestUserUpdateBillingOrganizationUsers(SimpleTestCase):
    @patch("posthog.models.user.is_cloud", return_value=True)
    @patch("posthog.models.user.get_cached_instance_license")
    def test_user_sync_does_not_send_membership_data(self, get_license: MagicMock, is_cloud: MagicMock) -> None:
        get_license.return_value = License(key="example::secret", plan="enterprise")
        with patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected billing HTTP")):
            User(email="member@example.com").update_billing_organization_users(Organization(name="Local"))
