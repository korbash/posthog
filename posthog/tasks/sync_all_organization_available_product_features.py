from collections.abc import Iterator
from typing import cast

from posthog.models.organization import Organization


def sync_all_organization_available_product_features() -> None:
    for organization in cast(
        Iterator[Organization], Organization.objects.only("id", "available_product_features").iterator()
    ):
        organization.update_available_product_features(save=True)
