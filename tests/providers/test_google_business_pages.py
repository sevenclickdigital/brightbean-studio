"""Tests for GoogleBusinessProvider.get_user_pages."""

from unittest.mock import MagicMock, patch

from providers.google_business import LOCATION_READ_MASK, GoogleBusinessProvider


def _make_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _responses(*payloads: dict):
    """A ``_request`` side effect returning each payload in call order."""
    return [_make_response(payload) for payload in payloads]


class TestGetUserPages:
    @patch.object(GoogleBusinessProvider, "_request")
    def test_returns_every_location_across_every_account(self, mock_request):
        """The picker's whole point: one grant can reach several accounts.

        Stopping at the first account would hide locations the user is entitled
        to connect, which is the single-account behaviour this hook exists to
        replace.
        """
        mock_request.side_effect = _responses(
            {"accounts": [{"name": "accounts/1"}, {"name": "accounts/2"}]},
            {
                "locations": [
                    {
                        "name": "locations/11",
                        "title": "Downtown",
                        "storefrontAddress": {"addressLines": ["1 Main St", "Suite 2"]},
                    }
                ]
            },
            {"locations": [{"name": "locations/22", "title": "Airport"}]},
        )

        pages = GoogleBusinessProvider().get_user_pages("token-xyz")

        assert pages == [
            {
                "id": "locations/11",
                "name": "Downtown",
                "access_token": "",
                "category": "1 Main St, Suite 2",
                "picture": None,
                "followers_count": 0,
                "account_id": "accounts/1",
            },
            {
                "id": "locations/22",
                "name": "Airport",
                "access_token": "",
                "category": "",
                "picture": None,
                "followers_count": 0,
                "account_id": "accounts/2",
            },
        ]

    @patch.object(GoogleBusinessProvider, "_request")
    def test_the_id_is_the_location_not_the_account(self, mock_request):
        """``publish_post`` posts to ``id``, so it has to be what the user picked.

        Returning the account here would publish to whichever location the API
        listed first, silently ignoring the choice — the bug the picker exists
        to prevent, and one that only shows up in production.
        """
        mock_request.side_effect = _responses(
            {"accounts": [{"name": "accounts/42"}]},
            {"locations": [{"name": "locations/99", "title": "Second Branch"}]},
        )

        (page,) = GoogleBusinessProvider().get_user_pages("token")

        assert page["id"] == "locations/99"
        assert page["account_id"] == "accounts/42"

    @patch.object(GoogleBusinessProvider, "_request")
    def test_locations_are_listed_with_the_read_mask(self, mock_request):
        """``locations.list`` 400s without ``readMask``, so the picker needs it too."""
        mock_request.side_effect = _responses(
            {"accounts": [{"name": "accounts/1"}]},
            {"locations": []},
        )

        GoogleBusinessProvider().get_user_pages("token")

        assert mock_request.call_args.kwargs["params"] == {"readMask": LOCATION_READ_MASK}

    @patch.object(GoogleBusinessProvider, "_request")
    def test_falls_back_to_the_resource_name_when_a_location_is_untitled(self, mock_request):
        """An empty label would render as a blank, unpickable row."""
        mock_request.side_effect = _responses(
            {"accounts": [{"name": "accounts/1"}]},
            {"locations": [{"name": "locations/7"}]},
        )

        (page,) = GoogleBusinessProvider().get_user_pages("token")

        assert page["name"] == "locations/7"

    @patch.object(GoogleBusinessProvider, "_request")
    def test_skips_entries_with_no_resource_name(self, mock_request):
        """A nameless location cannot be stored or published to, so it is dropped.

        Keeping it would offer a row that fails only once the user connects it.
        """
        mock_request.side_effect = _responses(
            {"accounts": [{"name": ""}, {"name": "accounts/1"}]},
            {"locations": [{"title": "No resource name"}, {"name": "locations/5", "title": "Real"}]},
        )

        pages = GoogleBusinessProvider().get_user_pages("token")

        assert [page["id"] for page in pages] == ["locations/5"]

    @patch.object(GoogleBusinessProvider, "_request")
    def test_returns_nothing_when_the_grant_reaches_no_accounts(self, mock_request):
        """The caller renders an empty picker rather than raising."""
        mock_request.side_effect = _responses({})

        assert GoogleBusinessProvider().get_user_pages("token") == []
