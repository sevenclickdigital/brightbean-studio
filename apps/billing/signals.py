"""Keep the cached per-workspace disk figure honest.

``storage_usage.used_storage_bytes`` caches a ``SUM`` because it runs on
every upload. These handlers drop that entry whenever the sum could have
moved, so the cache never outlives the truth by more than the request that
changed it.

Both directions matter, for different reasons:

- **create** — without this, a workspace at 89% could upload past 100%
  for the length of the TTL, which is the block this phase exists to
  enforce.
- **delete** — without this, a user who follows the 90% warning and
  deletes files sees no change and is still refused, which is worse than
  the warning never appearing.

``post_delete`` fires per row even for a queryset delete, and the
``file_size`` is still readable on the instance, so bulk cleanups (the
orphaned-media sweep) are covered too.
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_init, post_save
from django.dispatch import receiver

from apps.billing.storage_usage import invalidate_usage
from apps.media_library.models import MediaAsset

#: Where we stash the workspace an asset was loaded with, so a save that
#: moves it between workspaces can invalidate the one it left as well as
#: the one it joined. Reading ``instance.workspace_id`` in ``post_save``
#: only ever yields the new value.
_LOADED_WORKSPACE_ATTR = "_billing_loaded_workspace_id"


@receiver(post_init, sender=MediaAsset, dispatch_uid="billing_track_loaded_workspace")
def _track_loaded_workspace(sender, instance, **_):
    setattr(instance, _LOADED_WORKSPACE_ATTR, instance.workspace_id)


@receiver(post_save, sender=MediaAsset, dispatch_uid="billing_invalidate_usage_on_asset_save")
def _invalidate_on_asset_save(sender, instance, **_):
    """Bust on every save, not just creation.

    A version restore rewrites ``file_size`` on an existing row
    (``media_library.services.restore_version``), so "created only" would
    miss a real change in usage. Moving an asset to another workspace
    changes two sums, hence the previous workspace is busted too.
    """
    previous = getattr(instance, _LOADED_WORKSPACE_ATTR, None)
    if previous is not None and previous != instance.workspace_id:
        invalidate_usage(previous)
    setattr(instance, _LOADED_WORKSPACE_ATTR, instance.workspace_id)
    invalidate_usage(instance.workspace_id)


@receiver(post_delete, sender=MediaAsset, dispatch_uid="billing_invalidate_usage_on_asset_delete")
def _invalidate_on_asset_delete(sender, instance, **_):
    invalidate_usage(instance.workspace_id)
