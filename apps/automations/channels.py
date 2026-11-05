"""What each platform can automate, for the "new rule" chooser.

The chooser lists every connected account and says, per account, whether a
rule can run on it and why not when it cannot. That question is asked in one
place here rather than as a template branch, because the answer is a property
of the platform's API — a webhook to match on and an endpoint to answer
through — and not of how the list happens to be drawn.

Platforms that cannot automate are listed and disabled rather than hidden: a
Google Business Profile missing from the chooser reads as a bug, while one
marked "review replies — coming soon" reads as a roadmap.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from apps.automations import matching

#: Platforms whose automation is a comment on a post. These have both halves
#: the engine needs, and are the only ones a rule can run on today. Sourced
#: from ``matching`` so the chooser and the matcher can never disagree.
COMMENT_PLATFORMS = matching.COMMENT_PLATFORMS

#: Platforms where automation is understood but unbuilt, with the shape it
#: would take. ``InboxMessage.MessageType.REVIEW`` exists already, but no
#: provider produces one: Google Business Profile neither fetches reviews nor
#: answers them, so this is a feature to build, not a flag to flip.
PLANNED_PLATFORMS = {
    "google_business": _("Review replies — coming soon"),
}

#: Said of everything else. Not a failure — most platforms have no comment
#: webhook at all, and nothing is pending for them.
UNSUPPORTED_REASON = _("This channel doesn't support automations")


def channel_support(platform: str) -> tuple[bool, str]:
    """``(can_automate, reason)`` for one platform.

    ``reason`` is empty when the platform is supported — there is nothing to
    explain about a channel that works.
    """
    if platform in COMMENT_PLATFORMS:
        return True, ""
    if platform in PLANNED_PLATFORMS:
        return False, str(PLANNED_PLATFORMS[platform])
    return False, str(UNSUPPORTED_REASON)


def choosable_accounts(workspace):
    """Every connected account in ``workspace``, annotated for the chooser.

    Supported accounts come first so the ones a rule can actually use are not
    buried under a list of disabled ones; within each group the order is the
    account list's own.
    """
    from apps.social_accounts.models import SocialAccount

    accounts = (
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .order_by("platform", "account_name")
    )

    annotated = []
    for account in accounts:
        can_automate, reason = channel_support(account.platform)
        annotated.append(
            {
                "account": account,
                "can_automate": can_automate,
                "reason": reason,
            }
        )

    annotated.sort(key=lambda entry: not entry["can_automate"])
    return annotated
