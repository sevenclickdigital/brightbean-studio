"""Background tasks for the billing app."""

import logging

from background_task import background

logger = logging.getLogger(__name__)

# How often the courtesy-expiry sweep runs; registered on a repeating
# schedule by apps.billing.apps.BillingConfig.
#
# Daily, not hourly: the deadline is a *date*
# (``OrganizationBillingProfile.courtesy_expires_on``), so the finest
# distinction the data can make is one day. Running more often would only
# re-ask a question whose answer cannot have changed.
COURTESY_EXPIRY_INTERVAL_SECONDS = 24 * 60 * 60  # daily


@background(schedule=0)
def expire_courtesies():
    """Return accounts whose free period has ended to being billed.

    Only billing state moves — see ``services.expire_due_courtesies``.
    Nothing is deleted and no access is withdrawn: an expired courtesy
    means the account starts paying, not that it stops working.

    Logged at ``warning`` when it actually expires something, because
    somebody is about to be charged who was not being charged yesterday
    and that should be findable in the logs without a database query.
    """
    from apps.billing import services

    expired = services.expire_due_courtesies()
    if expired:
        logger.warning("Courtesy expired for %d account(s); they are billable again", expired)
    return expired
