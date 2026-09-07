import json

from posthog.test.base import APIBaseTest
from unittest import mock
from unittest.mock import MagicMock

from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from parameterized import parameterized

from posthog.models import UserHomeSettings
from posthog.utils import get_context_for_template


class TestGetContextForTemplate(APIBaseTest):
    def test_get_context_for_template(self):
        with self.settings(STRIPE_PUBLIC_KEY=None, PERSISTED_FEATURE_FLAGS=["the_persisted_flags"]):
            actual = get_context_for_template(
                "layout",
                MagicMock(),
            )

        assert actual == {
            "git_rev": mock.ANY,
            "js_capture_time_to_see_data": False,
            "js_url": "http://localhost:8234",
            "opt_out_capture": True,
            "posthog_app_context": '{"persisted_feature_flags": ["the_persisted_flags"], "anonymous": false}',
            "posthog_bootstrap": "{}",
            "posthog_js_uuid_version": "v7",
            "region": None,
            "self_capture": False,
        }

    def test_picks_up_stripe_public_key_from_environment(self):
        with self.settings(STRIPE_PUBLIC_KEY="pk_test_12345"):
            actual = get_context_for_template(
                "layout",
                MagicMock(),
            )

        assert actual["stripe_public_key"] == "pk_test_12345"

    @override_settings(CLOUD_DEPLOYMENT="E2E", DEBUG=False, E2E_TESTING=True, OPT_OUT_CAPTURE=False, TEST=False)
    def test_e2e_context_omits_posthog_cloud_credentials(self):
        actual = get_context_for_template("layout", MagicMock())

        assert actual["opt_out_capture"] is True
        assert actual["e2e_testing"] is True
        assert "js_posthog_api_key" not in actual
        assert "js_posthog_host" not in actual

    @parameterized.expand(
        [
            ("configured", {"pathname": "/dashboard/42", "pinned": True, "title": "Default dashboard"}),
            ("not_configured", None),
            ("empty_is_cleared", {}),
        ]
    )
    def test_bootstraps_configured_homepage_into_app_context(self, _name, stored_homepage):
        if stored_homepage is not None:
            UserHomeSettings.objects.create(user=self.user, team=self.team, homepage=stored_homepage)

        request = RequestFactory().get("/")
        SessionMiddleware(lambda _request: HttpResponse()).process_request(request)
        request.user = self.user

        actual = get_context_for_template("layout", request)

        app_context = json.loads(actual["posthog_app_context"])
        assert app_context["homepage"] == (stored_homepage or None)

    @parameterized.expand(
        [
            ("anonymous_is_always_light", None, "light"),
            ("missing_theme_mode_means_light", "", "light"),
            ("dark", "dark", "dark"),
            ("system_defers_to_the_os", "system", "system"),
        ]
    )
    def test_boot_theme_mirrors_theme_logic(self, _name, theme_mode, expected):
        request = RequestFactory().get("/")
        SessionMiddleware(lambda _request: HttpResponse()).process_request(request)
        if theme_mode is None:
            request.user = AnonymousUser()
        else:
            self.user.theme_mode = theme_mode or None
            self.user.save()
            request.user = self.user

        actual = get_context_for_template("index.html", request)

        assert actual["boot_theme"] == expected
