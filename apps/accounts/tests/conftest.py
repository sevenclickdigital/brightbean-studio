import pytest


@pytest.fixture(autouse=True)
def _english_responses(settings):
    """Render these tests' pages in English.

    The interface is served in Portuguese by default, so a page asserted on
    by its English wording would fail on the translation rather than on the
    behaviour under test — and asserting on the Portuguese instead would only
    move the problem, breaking the test whenever a translator picks a better
    word. Pinning the language keeps the assertions about what the gate does.

    LANGUAGE_CODE and not translation.activate(): LocaleMiddleware resolves
    the language per request and would overwrite anything set on the thread
    beforehand. This changes what the middleware itself falls back to.
    """
    settings.LANGUAGE_CODE = "en-us"
