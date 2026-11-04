"""Tests for the Asaas HTTP client.

``httpx.request`` is patched at the boundary, so these assert on what we
put on the wire and how we read what comes back — never on a real call.
No credentials are needed or used: the key is a fake set through
``override_settings``.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from unittest import mock

import httpx
from django.test import SimpleTestCase, override_settings

from apps.billing.gateways.asaas import (
    AsaasAPIError,
    AsaasClient,
    AsaasNotConfigured,
    AsaasTransportError,
    cents_to_value,
)

API_BASE = "https://api-sandbox.asaas.com/v3"


def _response(status=200, json_body=None, content=b"{}"):
    """A stand-in httpx.Response with just what the client reads."""
    response = mock.Mock(spec=["status_code", "json", "content"])
    response.status_code = status
    response.content = content if json_body is None else b"{...}"
    if json_body is None:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = json_body
    return response


@override_settings(ASAAS_API_KEY="fake_key_for_tests", ASAAS_API_BASE=API_BASE)
class ClientRequestTests(SimpleTestCase):
    def test_api_key_goes_in_the_access_token_header(self):
        """Asaas authenticates on this bespoke header, not Bearer auth."""
        with mock.patch("httpx.request", return_value=_response(json_body={"id": "cus_1"})) as request:
            AsaasClient().get_customer("cus_1")
        _args, kwargs = request.call_args
        self.assertEqual(kwargs["headers"]["access_token"], "fake_key_for_tests")
        self.assertNotIn("Authorization", kwargs["headers"])

    def test_url_is_built_from_the_configured_base(self):
        with mock.patch("httpx.request", return_value=_response(json_body={"id": "cus_1"})) as request:
            AsaasClient().get_customer("cus_1")
        args, _kwargs = request.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[1], f"{API_BASE}/customers/cus_1")

    def test_production_base_is_honoured(self):
        with override_settings(ASAAS_API_BASE="https://api.asaas.com/v3"):
            with mock.patch("httpx.request", return_value=_response(json_body={})) as request:
                AsaasClient().get_customer("cus_1")
        args, _kwargs = request.call_args
        self.assertTrue(args[1].startswith("https://api.asaas.com/v3/"))

    def test_trailing_slash_on_base_does_not_double(self):
        with override_settings(ASAAS_API_BASE=f"{API_BASE}/"):
            with mock.patch("httpx.request", return_value=_response(json_body={})) as request:
                AsaasClient().get_customer("cus_1")
        args, _kwargs = request.call_args
        self.assertNotIn("//customers", args[1].replace("https://", ""))


class ConfigurationTests(SimpleTestCase):
    @override_settings(ASAAS_API_KEY="", ASAAS_API_BASE=API_BASE)
    def test_missing_key_refuses_without_calling_out(self):
        """The default state today: no credentials issued yet."""
        with mock.patch("httpx.request") as request:
            with self.assertRaises(AsaasNotConfigured):
                AsaasClient().get_customer("cus_1")
        request.assert_not_called()

    @override_settings(ASAAS_API_KEY="fake", ASAAS_API_BASE="")
    def test_missing_base_refuses(self):
        with self.assertRaises(AsaasNotConfigured):
            AsaasClient().get_customer("cus_1")

    @override_settings(ASAAS_API_KEY="", ASAAS_API_BASE=API_BASE)
    def test_is_configured_reports_false_without_key(self):
        self.assertFalse(AsaasClient().is_configured)

    @override_settings(ASAAS_API_KEY="fake", ASAAS_API_BASE=API_BASE)
    def test_is_configured_reports_true_with_both(self):
        self.assertTrue(AsaasClient().is_configured)


@override_settings(ASAAS_API_KEY="fake", ASAAS_API_BASE=API_BASE)
class ErrorHandlingTests(SimpleTestCase):
    def test_network_failure_becomes_transport_error(self):
        with mock.patch("httpx.request", side_effect=httpx.ConnectError("boom")):
            with self.assertRaises(AsaasTransportError):
                AsaasClient().get_customer("cus_1")

    def test_timeout_becomes_transport_error(self):
        with mock.patch("httpx.request", side_effect=httpx.ReadTimeout("slow")):
            with self.assertRaises(AsaasTransportError):
                AsaasClient().get_customer("cus_1")

    def test_error_status_carries_code_and_asaas_errors(self):
        body = {"errors": [{"code": "invalid_cpfCnpj", "description": "CPF inválido"}]}
        with mock.patch("httpx.request", return_value=_response(status=400, json_body=body)):
            with self.assertRaises(AsaasAPIError) as ctx:
                AsaasClient().get_customer("cus_1")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.errors[0]["code"], "invalid_cpfCnpj")

    def test_client_error_is_not_retryable(self):
        with mock.patch("httpx.request", return_value=_response(status=400, json_body={})):
            with self.assertRaises(AsaasAPIError) as ctx:
                AsaasClient().get_customer("cus_1")
        self.assertFalse(ctx.exception.is_retryable)

    def test_server_error_is_retryable(self):
        with mock.patch("httpx.request", return_value=_response(status=503, json_body={})):
            with self.assertRaises(AsaasAPIError) as ctx:
                AsaasClient().get_customer("cus_1")
        self.assertTrue(ctx.exception.is_retryable)

    def test_rate_limit_is_retryable(self):
        with mock.patch("httpx.request", return_value=_response(status=429, json_body={})):
            with self.assertRaises(AsaasAPIError) as ctx:
                AsaasClient().get_customer("cus_1")
        self.assertTrue(ctx.exception.is_retryable)

    def test_html_error_body_still_reports_the_status(self):
        """A 502 from their edge is HTML; the status must survive it."""
        with mock.patch("httpx.request", return_value=_response(status=502, json_body=None)):
            with self.assertRaises(AsaasAPIError) as ctx:
                AsaasClient().get_customer("cus_1")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(ctx.exception.errors, [])

    def test_empty_body_on_success_returns_empty_dict(self):
        with mock.patch("httpx.request", return_value=_response(status=200, json_body=None, content=b"")):
            self.assertEqual(AsaasClient().cancel_subscription("sub_1"), {})

    def test_api_key_is_never_logged(self):
        with mock.patch("httpx.request", side_effect=httpx.ConnectError("boom")):
            with self.assertLogs("apps.billing.gateways.asaas", level="WARNING") as logs:
                with self.assertRaises(AsaasTransportError):
                    AsaasClient(api_key="sekrit_key").get_customer("cus_1")
        self.assertNotIn("sekrit_key", "\n".join(logs.output))


@override_settings(ASAAS_API_KEY="fake", ASAAS_API_BASE=API_BASE)
class CustomerTests(SimpleTestCase):
    def test_create_customer_posts_name_and_external_reference(self):
        with mock.patch("httpx.request", return_value=_response(json_body={"id": "cus_1"})) as request:
            AsaasClient().create_customer(name="Acme", external_reference="org-uuid", email="a@b.com")
        args, kwargs = request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], f"{API_BASE}/customers")
        self.assertEqual(kwargs["json"]["name"], "Acme")
        self.assertEqual(kwargs["json"]["externalReference"], "org-uuid")
        self.assertEqual(kwargs["json"]["email"], "a@b.com")

    def test_blank_optional_fields_are_omitted_not_nulled(self):
        """Asaas 400s on an explicit null for these."""
        with mock.patch("httpx.request", return_value=_response(json_body={"id": "cus_1"})) as request:
            AsaasClient().create_customer(name="Acme", external_reference="org-uuid")
        _args, kwargs = request.call_args
        self.assertNotIn("email", kwargs["json"])
        self.assertNotIn("cpfCnpj", kwargs["json"])

    def test_find_customer_by_reference_filters_and_unwraps_the_list(self):
        body = {"object": "list", "hasMore": False, "totalCount": 1, "data": [{"id": "cus_9"}]}
        with mock.patch("httpx.request", return_value=_response(json_body=body)) as request:
            found = AsaasClient().find_customer_by_reference("org-uuid")
        _args, kwargs = request.call_args
        self.assertEqual(kwargs["params"]["externalReference"], "org-uuid")
        self.assertEqual(found["id"], "cus_9")

    def test_find_customer_returns_none_on_empty_list(self):
        body = {"object": "list", "totalCount": 0, "data": []}
        with mock.patch("httpx.request", return_value=_response(json_body=body)):
            self.assertIsNone(AsaasClient().find_customer_by_reference("org-uuid"))


@override_settings(ASAAS_API_KEY="fake", ASAAS_API_BASE=API_BASE)
class SubscriptionTests(SimpleTestCase):
    def _create(self, **overrides):
        kwargs = {
            "customer_id": "cus_1",
            "value_cents": 5000,
            "external_reference": "ws-uuid",
        }
        kwargs.update(overrides)
        with mock.patch("httpx.request", return_value=_response(json_body={"id": "sub_1"})) as request:
            AsaasClient().create_subscription(**kwargs)
        return request.call_args

    def test_posts_to_subscriptions(self):
        args, _kwargs = self._create()
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], f"{API_BASE}/subscriptions")

    def test_cents_are_sent_as_decimal_reais(self):
        """R$ 50,00 is 5000 cents here and 50.00 on the wire."""
        _args, kwargs = self._create(value_cents=5000)
        self.assertEqual(Decimal(kwargs["json"]["value"]), Decimal("50.00"))

    def test_odd_cents_do_not_lose_a_hundredth(self):
        _args, kwargs = self._create(value_cents=12345)
        self.assertEqual(Decimal(kwargs["json"]["value"]), Decimal("123.45"))

    def test_cycle_is_monthly_and_billing_type_is_open(self):
        _args, kwargs = self._create()
        self.assertEqual(kwargs["json"]["cycle"], "MONTHLY")
        self.assertEqual(kwargs["json"]["billingType"], "UNDEFINED")

    def test_external_reference_is_the_workspace(self):
        _args, kwargs = self._create()
        self.assertEqual(kwargs["json"]["externalReference"], "ws-uuid")

    def test_next_due_date_defaults_to_today_in_iso(self):
        _args, kwargs = self._create()
        self.assertRegex(kwargs["json"]["nextDueDate"], r"^\d{4}-\d{2}-\d{2}$")

    def test_explicit_due_date_is_sent_as_a_plain_date(self):
        _args, kwargs = self._create(next_due_date=datetime.date(2026, 3, 9))
        self.assertEqual(kwargs["json"]["nextDueDate"], "2026-03-09")

    def test_description_is_truncated_to_the_api_limit(self):
        _args, kwargs = self._create(description="x" * 900)
        self.assertEqual(len(kwargs["json"]["description"]), 500)

    def test_cancel_uses_delete_on_the_subscription(self):
        with mock.patch(
            "httpx.request",
            return_value=_response(json_body={"deleted": True, "id": "sub_1"}),
        ) as request:
            result = AsaasClient().cancel_subscription("sub_1")
        args, _kwargs = request.call_args
        self.assertEqual(args[0], "DELETE")
        self.assertEqual(args[1], f"{API_BASE}/subscriptions/sub_1")
        self.assertTrue(result["deleted"])


class MoneyConversionTests(SimpleTestCase):
    def test_whole_reais(self):
        self.assertEqual(cents_to_value(5000), Decimal("50"))

    def test_fractional_reais_are_exact(self):
        self.assertEqual(cents_to_value(1), Decimal("0.01"))

    def test_conversion_returns_decimal_not_float(self):
        self.assertIsInstance(cents_to_value(5000), Decimal)
