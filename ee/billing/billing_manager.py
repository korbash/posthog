import hmac
import time
import hashlib
from datetime import UTC, datetime, timedelta
from enum import Enum, StrEnum
from typing import Any, Literal, Optional, cast

from django.conf import settings
from django.utils import timezone

import jwt
import requests
import structlog
from requests import JSONDecodeError
from rest_framework.exceptions import NotAuthenticated

from posthog.dataclasses import frozen
from posthog.event_usage import report_user_action
from posthog.models import Organization
from posthog.models.organization import OrganizationMembership, ProductFeature
from posthog.models.user import User

from ee.billing.billing_types import BillingProvider, BillingStatus
from ee.models import License

logger = structlog.get_logger(__name__)

BILLING_PROVIDER_WEBHOOK_SIGNATURE_HEADER = "X-PostHog-Billing-Provider-Signature"
BILLING_PROVIDER_WEBHOOK_TIMESTAMP_HEADER = "X-PostHog-Billing-Provider-Timestamp"
BILLING_PROVIDER_WEBHOOK_SIGNATURE_VERSION = "sha256"


StartupProgramLabel = Literal["Startup", "YC"]


class PrepaidCreditState(StrEnum):
    NONE = "none"
    PENDING = "pending"
    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    EXPIRED = "expired"


class FundingStatusUnavailable(Exception):
    pass


@frozen
class OrganizationFundingStatus:
    startup_program_label: StartupProgramLabel | None
    prepaid_credit_state: PrepaidCreditState


class BillingAPIErrorCodes(Enum):
    OPEN_INVOICES_ERROR = "open_invoices_error"


class BillingServiceOpenInvoicesError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _get_user_organization_role(user: User, organization: Organization) -> Optional[str]:
    """
    Get a user role display string in a given organization, if membership doesn't exist return None.
    """
    try:
        membership = user.organization_memberships.get(organization=organization)
        return membership.get_level_display()
    except OrganizationMembership.DoesNotExist:
        return None


def build_billing_token(
    license: Optional[License],
    organization: Optional[Organization],
    user: Optional[User] = None,
    authorizer_actor: Optional[User] = None,
    billing_provider: BillingProvider | None = None,
    service_action: str | None = None,
) -> str:
    """
    Build the JWT token to authenticate with the Billing system.

    Allows doing privilege escalation with the `authorizer_actor` parameter, in that case the distinct_id
    will be that of the user, but the role will be that of the authorizer_actor.

    `service_action` marks a token minted by a backend job for one specific service-to-service
    endpoint (e.g. "signals_pr_dispute"); billing rejects calls to such endpoints from tokens
    without the matching claim, so tokens minted for user-initiated calls can't reach them.

    Raises NotAuthenticated if the authorizer_actor (or user in case there's no authorizer_actor) are not
    part of the organization.
    """
    if not organization or not license:
        raise NotAuthenticated()

    license_id = license.key.split("::")[0]
    license_secret = license.key.split("::")[1]

    payload = {
        "exp": datetime.now(tz=UTC) + timedelta(minutes=15),
        "id": license_id,
        "organization_id": str(organization.id),
        "organization_name": organization.name,
        "aud": "posthog:license-key",
    }

    if user:
        authorizer_actor = authorizer_actor or user

        payload["distinct_id"] = str(user.distinct_id)
        authorizer_role = _get_user_organization_role(authorizer_actor, organization)

        if authorizer_role:
            payload["organization_role"] = authorizer_role
        else:
            raise NotAuthenticated(f"Authorizer is not part of organization")

        if authorizer_actor != user:
            # We've done a privilege escalation
            report_user_action(
                authorizer_actor,
                "$billing_privilege_escalation",
                properties={
                    "target_user_id": user.id,
                    "target_distinct_id": str(user.distinct_id),
                    "target_email": user.email,
                    "action": "update_billing",
                },
            )
            payload["original_role"] = _get_user_organization_role(user, organization)

    if billing_provider:
        payload["billing_provider"] = billing_provider.value

    if service_action:
        payload["service_action"] = service_action

    encoded_jwt = jwt.encode(
        payload,
        license_secret,
        algorithm="HS256",
    )

    return encoded_jwt


def _compute_webhook_signature(secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 over "<timestamp>.<body>", hex-encoded."""
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha256)
    mac.update(f"{timestamp}.".encode())
    mac.update(body)
    return mac.digest().hex()


def build_billing_provider_webhook_signature_headers(body: bytes) -> dict[str, str]:
    secret = getattr(settings, "BILLING_PROVIDER_WEBHOOK_SECRET", "")
    if not secret:
        raise ValueError("BILLING_PROVIDER_WEBHOOK_SECRET is not configured")

    timestamp = int(time.time())
    digest = _compute_webhook_signature(secret, timestamp, body)
    return {
        BILLING_PROVIDER_WEBHOOK_SIGNATURE_HEADER: f"{BILLING_PROVIDER_WEBHOOK_SIGNATURE_VERSION}={digest}",
        BILLING_PROVIDER_WEBHOOK_TIMESTAMP_HEADER: str(timestamp),
    }


def handle_billing_service_error(res: requests.Response, valid_codes=(200, 201, 404, 401)) -> None:
    if res.status_code not in valid_codes:
        logger.error(f"Billing service returned bad status code: {res.status_code}, body: {res.text}")
        try:
            response = res.json()
            raise Exception(f"Billing service returned bad status code: {res.status_code}", f"body:", response)
        except JSONDecodeError:
            raise Exception(f"Billing service returned bad status code: {res.status_code}", f"body:", res.text)


def _parse_funding_status(data: object) -> OrganizationFundingStatus:
    if not isinstance(data, dict):
        raise FundingStatusUnavailable("Billing returned an invalid funding status response")

    if "startup_program_label" not in data:
        raise FundingStatusUnavailable("Billing returned an invalid startup program label")
    raw_startup_program_label = data.get("startup_program_label")
    if raw_startup_program_label not in (None, "Startup", "YC"):
        raise FundingStatusUnavailable("Billing returned an invalid startup program label")
    startup_program_label = cast(StartupProgramLabel | None, raw_startup_program_label)

    raw_prepaid_credit_state = data.get("prepaid_credit_state")
    if not isinstance(raw_prepaid_credit_state, str):
        raise FundingStatusUnavailable("Billing returned an invalid prepaid credit state")
    try:
        prepaid_credit_state = PrepaidCreditState(raw_prepaid_credit_state)
    except ValueError as error:
        raise FundingStatusUnavailable("Billing returned an invalid prepaid credit state") from error

    return OrganizationFundingStatus(
        startup_program_label=startup_program_label,
        prepaid_credit_state=prepaid_credit_state,
    )


class BillingManager:
    """Local billing compatibility API; no operation contacts a billing provider."""

    def __init__(self, license: License | None, user: User | None = None, ip_address: str | None = None) -> None:
        self.license = license
        self.user = user
        self.ip_address = ip_address

    def get_billing(
        self,
        organization: Organization | None,
        query_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._get_default_billing_response(organization)

    def update_billing(
        self, organization: Organization, data: dict[str, Any], authorizer_actor: Optional[User] = None
    ) -> None:
        return None

    def update_available_product_features(self, organization: Organization) -> list[ProductFeature]:
        return organization.update_available_product_features(save=True)

    def update_billing_organization_users(self, organization: Organization) -> None:
        return None

    def activate_subscription(self, organization: Organization, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True}

    def deactivate_products(self, organization: Organization, products: str) -> None:
        return None

    def get_funding_status(self, organization: Organization) -> OrganizationFundingStatus:
        return OrganizationFundingStatus(startup_program_label=None, prepaid_credit_state=PrepaidCreditState.NONE)

    def _get_default_billing_response(self, organization: Organization | None) -> dict[str, Any]:
        return {
            **self._get_billing(organization)["customer"],
            "stripe_portal_url": self._get_stripe_portal_url(organization),
        }

    def get_default_products(self, organization: Organization | None) -> dict[str, Any]:
        return {"products": self._get_products(organization)}

    def update_license_details(self, billing_status: BillingStatus) -> License | None:
        return self.license

    def _get_billing(
        self, organization: Organization | None, query_params: dict[str, Any] | None = None
    ) -> BillingStatus:
        period_start = timezone.now().astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        period_end = (period_start + timedelta(days=32)).replace(day=1)
        return {
            "license": {"type": "self-hosted"},
            "customer": {
                "customer_id": organization.customer_id if organization else None,
                "deactivated": False,
                "has_active_subscription": False,
                "billing_period": {
                    "current_period_start": period_start.isoformat(),
                    "current_period_end": period_end.isoformat(),
                    "interval": "month",
                },
                "available_product_features": (organization.available_product_features or []) if organization else [],
                "current_total_amount_usd": "0",
                "current_total_amount_usd_after_discount": "0",
                "projected_total_amount_usd_with_limit_after_discount": "0",
                "products": [],
                "custom_limits_usd": {},
                "usage_summary": {},
                "free_trial_until": None,
                "discount_percent": None,
                "discount_amount_usd": None,
                "customer_trust_scores": {},
            },
        }

    def _get_stripe_portal_url(self, organization: Organization | None) -> str:
        return "/organization/billing"

    def _get_products(self, organization: Organization | None) -> list[dict[str, Any]]:
        return []

    def update_org_details(self, organization: Organization, billing_status: BillingStatus) -> Organization:
        return organization

    def get_auth_headers(
        self,
        organization: Organization,
        billing_provider: BillingProvider | None = None,
        authorizer_actor: User | None = None,
        service_action: str | None = None,
    ) -> dict[str, str]:
        if not self.license:
            raise NotAuthenticated("No license found")
        token = build_billing_token(
            self.license,
            organization,
            self.user,
            authorizer_actor=authorizer_actor,
            billing_provider=billing_provider,
            service_action=service_action,
        )
        headers = {"Authorization": f"Bearer {token}"}
        if self.ip_address:
            headers["X-PostHog-Actor-IP"] = self.ip_address
        return headers

    def get_invoices(self, organization: Organization, status: str | None = None) -> dict[str, Any]:
        return {"count": 0, "results": [], "portal_url": self._get_stripe_portal_url(organization)}

    def credits_overview(self, organization: Organization) -> dict[str, Any]:
        return {
            "eligible": False,
            "estimated_monthly_credit_amount_usd": None,
            "status": "none",
            "invoice_url": None,
            "collection_method": None,
            "cc_last_four": None,
            "email": None,
            "credit_brackets": [],
        }

    def purchase_credits(self, organization: Organization, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True}

    def dispute_signals_pr(self, organization: Organization, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "credit_amount_usd": "0", "zero_reason": "self_hosted"}

    def activate_trial(self, organization: Organization, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True}

    def cancel_trial(self, organization: Organization, data: dict[str, Any]) -> None:
        return None

    def authorize(self, organization: Organization, billing_provider: BillingProvider | None = None) -> dict[str, Any]:
        return {"success": True, "status": "success", "clientSecret": None}

    def authorize_status(self, organization: Organization, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "status": "success"}

    def deauthorize(self, organization: Organization, billing_provider: BillingProvider) -> dict[str, Any]:
        return {"success": True}

    def switch_plan(self, organization: Organization, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True}

    def apply_startup_program(self, organization: Organization, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True}

    def claim_coupon(self, organization: Organization, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True}

    def coupons_overview(self, organization: Organization) -> dict[str, Any]:
        return {"claimed_coupons": []}

    def get_billing_status_for_alerts(self, organization: Organization) -> dict[str, Any]:
        return dict(self._get_billing(organization))

    def get_usage_data(self, organization: Organization, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            "type": "timeseries",
            "customer_id": organization.customer_id or "",
            "results": [],
            "team_id_options": [],
        }

    def get_spend_data(self, organization: Organization, params: dict[str, Any]) -> dict[str, Any]:
        return self.get_usage_data(organization, params)

    def handle_billing_provider_webhook(
        self,
        event_type: str,
        event_data: dict[str, Any],
        organization: Organization,
        billing_provider: str,
    ) -> None:
        return None
