"""Tests for per-workspace disk usage, the 90% warning and the 100% block.

Behaviour and state only. Nothing here asserts on rendered text or
markup — the storage UI arrives in a later phase and its wording is not a
contract. The error message is checked for *existence and translation
machinery*, never for a specific sentence.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.billing import services
from apps.billing.models import WorkspaceAddon, WorkspaceSubscription
from apps.billing.storage_usage import (
    WARNING_PERCENT,
    WorkspaceStorageExceededError,
    enforce_workspace_storage,
    storage_status,
    used_storage_bytes,
)
from apps.media_library.models import MediaAsset
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

GB = WorkspaceAddon.BYTES_PER_GB


def _org(name="Acme"):
    return Organization.objects.create(name=name)


def _workspace(name="Workspace", org=None):
    return Workspace.objects.create(organization=org or _org(), name=name)


def _subscribe(workspace, *, base_bytes=10 * GB, status=WorkspaceSubscription.Status.ACTIVE):
    sub = services.ensure_subscription(workspace)
    sub.status = status
    sub.base_storage_bytes = base_bytes
    sub.save(update_fields=["status", "base_storage_bytes", "updated_at"])
    return sub


def _asset(workspace, size, *, org=None):
    """A MediaAsset of a given byte size, with no real file behind it.

    ``file`` is set to a bare string so no storage write happens — these
    tests are about the summed ``file_size`` column, not about bytes on
    disk.
    """
    asset = MediaAsset(
        organization=org or (workspace.organization if workspace else None),
        workspace=workspace,
        filename="f.jpg",
        media_type=MediaAsset.MediaType.IMAGE,
        file_size=size,
    )
    asset.file = "media_library/2026/01/f.jpg"
    asset.save()
    return asset


class UsageSumTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_empty_workspace_uses_nothing(self):
        self.assertEqual(used_storage_bytes(_workspace()), 0)

    def test_usage_is_the_sum_of_file_sizes(self):
        ws = _workspace()
        _asset(ws, 100)
        _asset(ws, 250)
        _asset(ws, 1)
        self.assertEqual(used_storage_bytes(ws, use_cache=False), 351)

    def test_another_workspace_assets_do_not_count(self):
        org = _org()
        mine = _workspace("Mine", org=org)
        theirs = _workspace("Theirs", org=org)
        _asset(mine, 100)
        _asset(theirs, 900)
        self.assertEqual(used_storage_bytes(mine, use_cache=False), 100)
        self.assertEqual(used_storage_bytes(theirs, use_cache=False), 900)

    def test_accepts_a_bare_workspace_id(self):
        ws = _workspace()
        _asset(ws, 42)
        self.assertEqual(used_storage_bytes(ws.pk, use_cache=False), 42)


class SharedMediaTests(TestCase):
    """Shared media (``workspace`` NULL) is the organization's, not a
    workspace's — the documented rule in ``storage_usage``."""

    def setUp(self):
        cache.clear()

    def test_shared_asset_counts_against_no_workspace(self):
        org = _org()
        ws = _workspace(org=org)
        _asset(None, 5 * GB, org=org)  # shared, org-wide
        self.assertEqual(used_storage_bytes(ws, use_cache=False), 0)

    def test_shared_asset_still_counts_against_the_organization(self):
        """The org cap is where shared bytes land, so they are not free."""
        from apps.media_library.quotas import used_storage_bytes as org_used

        org = _org()
        _asset(None, 5 * GB, org=org)
        self.assertEqual(org_used(org), 5 * GB)

    def test_shared_upload_is_not_blocked_by_a_full_workspace(self):
        org = _org()
        ws = _workspace(org=org)
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 1000)  # workspace is full
        # workspace=None → the workspace cap does not apply at all.
        enforce_workspace_storage(None, 10**9)


class StorageStatusTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_quota_is_base_plus_active_addons(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=10 * GB)
        addon = services.contract_storage_addon(ws, 5)
        addon.status = WorkspaceAddon.Status.ACTIVE
        addon.save(update_fields=["status", "updated_at"])
        self.assertEqual(storage_status(ws).limit_bytes, 15 * GB)

    def test_pending_addon_does_not_raise_the_quota(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=10 * GB)
        services.contract_storage_addon(ws, 5)  # left PENDING
        self.assertEqual(storage_status(ws).limit_bytes, 10 * GB)

    def test_remaining_and_percent(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 250)
        status = storage_status(ws, use_cache=False)
        self.assertEqual(status.used_bytes, 250)
        self.assertEqual(status.remaining_bytes, 750)
        self.assertEqual(status.percent_used, 25.0)
        self.assertFalse(status.is_near_limit)
        self.assertFalse(status.is_full)

    def test_remaining_floors_at_zero_when_over(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 1500)
        status = storage_status(ws, use_cache=False)
        self.assertEqual(status.remaining_bytes, 0)
        self.assertTrue(status.is_full)

    def test_workspace_without_subscription_has_no_quota(self):
        ws = _workspace()
        status = storage_status(ws)
        self.assertFalse(status.has_subscription)
        self.assertEqual(status.limit_bytes, 0)
        self.assertTrue(status.is_full)

    def test_courtesy_workspace_gets_its_quota(self):
        """Courtesy counts as active everywhere, disk included."""
        ws = _workspace()
        _subscribe(ws, base_bytes=10 * GB, status=WorkspaceSubscription.Status.COURTESY)
        self.assertEqual(storage_status(ws).limit_bytes, 10 * GB)


class NinetyPercentSignalTests(TestCase):
    """The 90% warning the product owner asked for."""

    def setUp(self):
        cache.clear()

    def test_below_the_threshold_is_not_near_limit(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 889)
        self.assertFalse(storage_status(ws, use_cache=False).is_near_limit)

    def test_exactly_at_the_threshold_is_near_limit(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 900)
        status = storage_status(ws, use_cache=False)
        self.assertEqual(status.percent_used, float(WARNING_PERCENT))
        self.assertTrue(status.is_near_limit)
        self.assertFalse(status.is_full)

    def test_between_threshold_and_full_is_near_limit(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 950)
        self.assertTrue(storage_status(ws, use_cache=False).is_near_limit)

    def test_full_is_not_near_limit_but_is_full(self):
        """Warning and block are mutually exclusive — the UI shows one."""
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 1000)
        status = storage_status(ws, use_cache=False)
        self.assertFalse(status.is_near_limit)
        self.assertTrue(status.is_full)

    def test_warning_percent_is_exposed_for_the_ui(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        self.assertEqual(storage_status(ws).warning_percent, WARNING_PERCENT)


class EnforcementTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_upload_below_the_limit_passes(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 500)
        enforce_workspace_storage(ws, 499)  # must not raise

    def test_upload_exactly_filling_the_quota_passes(self):
        """The limit is inclusive — 1000 of 1000 bytes is allowed."""
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 500)
        enforce_workspace_storage(ws, 500)

    def test_upload_one_byte_over_is_blocked(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 500)
        with self.assertRaises(WorkspaceStorageExceededError):
            enforce_workspace_storage(ws, 501)

    def test_blocked_error_carries_the_numbers(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 900)
        with self.assertRaises(WorkspaceStorageExceededError) as ctx:
            enforce_workspace_storage(ws, 300)
        exc = ctx.exception
        self.assertEqual(exc.used, 900)
        self.assertEqual(exc.limit, 1000)
        self.assertEqual(exc.attempted, 300)
        self.assertEqual(exc.workspace_id, ws.pk)

    def test_blocked_error_message_is_translatable_and_non_empty(self):
        """Behaviour, not wording: the message must render through gettext.

        Asserting the sentence itself would break the moment the copy or
        the active locale changes, which is exactly how this project got
        burned before.
        """
        ws = _workspace()
        _subscribe(ws, base_bytes=10 * GB)
        _asset(ws, 10 * GB)
        with self.assertRaises(WorkspaceStorageExceededError) as ctx:
            enforce_workspace_storage(ws, 1)
        rendered = str(ctx.exception.message)
        self.assertTrue(rendered.strip())
        # The quota is interpolated, so the user is told how much they have.
        self.assertIn("10", rendered)
        self.assertNotIn("%(limit)s", rendered)

    def test_workspace_at_full_blocks_any_further_byte(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 1000)
        with self.assertRaises(WorkspaceStorageExceededError):
            enforce_workspace_storage(ws, 1)

    def test_workspace_without_subscription_is_not_blocked_here(self):
        """Access gating is ``is_workspace_active``'s job, not the quota's.

        Blocking an uncontracted workspace here would take every
        pre-billing workspace offline the moment this ships.
        """
        ws = _workspace()
        _asset(ws, 10**12)
        enforce_workspace_storage(ws, 10**9)  # must not raise

    @override_settings(BILLING_STORAGE_QUOTA_ENABLED=False)
    def test_disabled_switch_skips_enforcement(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 1000)
        enforce_workspace_storage(ws, 10**9)


class AddonRaisesTheCeilingTests(TestCase):
    """The product promise: buy GB and the blocked upload goes through."""

    def setUp(self):
        cache.clear()

    def test_activating_an_addon_unblocks_a_previously_refused_upload(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=10 * GB)
        _asset(ws, 10 * GB)

        with self.assertRaises(WorkspaceStorageExceededError):
            enforce_workspace_storage(ws, 2 * GB)

        addon = services.contract_storage_addon(ws, 5)
        # Still PENDING — contracting alone must not hand out disk.
        with self.assertRaises(WorkspaceStorageExceededError):
            enforce_workspace_storage(ws, 2 * GB)

        addon.status = WorkspaceAddon.Status.ACTIVE
        addon.save(update_fields=["status", "updated_at"])

        enforce_workspace_storage(ws, 2 * GB)  # now fits
        self.assertEqual(storage_status(ws).limit_bytes, 15 * GB)

    def test_canceling_an_addon_lowers_the_ceiling_again(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=10 * GB)
        addon = services.contract_storage_addon(ws, 5)
        addon.status = WorkspaceAddon.Status.ACTIVE
        addon.save(update_fields=["status", "updated_at"])
        _asset(ws, 12 * GB)
        enforce_workspace_storage(ws, 1 * GB)

        addon.status = WorkspaceAddon.Status.CANCELED
        addon.save(update_fields=["status", "updated_at"])
        with self.assertRaises(WorkspaceStorageExceededError):
            enforce_workspace_storage(ws, 1 * GB)


class UsageCacheInvalidationTests(TestCase):
    """A cached SUM must never outlive the rows it summed."""

    def setUp(self):
        cache.clear()

    def test_creating_an_asset_invalidates_the_cached_usage(self):
        ws = _workspace()
        self.assertEqual(used_storage_bytes(ws), 0)  # primes the cache
        _asset(ws, 700)
        self.assertEqual(used_storage_bytes(ws), 700)

    def test_deleting_an_asset_invalidates_the_cached_usage(self):
        """Following the 90% advice has to actually free the quota up."""
        ws = _workspace()
        asset = _asset(ws, 700)
        self.assertEqual(used_storage_bytes(ws), 700)
        asset.delete()
        self.assertEqual(used_storage_bytes(ws), 0)

    def test_deleting_unblocks_a_full_workspace(self):
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        asset = _asset(ws, 1000)
        with self.assertRaises(WorkspaceStorageExceededError):
            enforce_workspace_storage(ws, 100)
        asset.delete()
        enforce_workspace_storage(ws, 100)

    def test_resizing_an_existing_asset_invalidates_the_cached_usage(self):
        ws = _workspace()
        asset = _asset(ws, 100)
        self.assertEqual(used_storage_bytes(ws), 100)
        asset.file_size = 900
        asset.save(update_fields=["file_size"])
        self.assertEqual(used_storage_bytes(ws), 900)

    def test_moving_an_asset_invalidates_both_workspaces(self):
        org = _org()
        source = _workspace("Source", org=org)
        target = _workspace("Target", org=org)
        asset = _asset(source, 500)
        self.assertEqual(used_storage_bytes(source), 500)
        self.assertEqual(used_storage_bytes(target), 0)

        asset.workspace = target
        asset.save(update_fields=["workspace"])

        self.assertEqual(used_storage_bytes(source), 0)
        self.assertEqual(used_storage_bytes(target), 500)

    @override_settings(BILLING_STORAGE_USAGE_CACHE_SECONDS=0)
    def test_zero_ttl_disables_caching(self):
        ws = _workspace()
        used_storage_bytes(ws)
        _asset(ws, 123)
        self.assertEqual(used_storage_bytes(ws), 123)


class BothQuotasApplyTests(TestCase):
    """``enforce_upload_quotas`` is the chokepoint for org AND workspace."""

    def setUp(self):
        cache.clear()

    def test_org_cap_still_blocks_even_with_workspace_headroom(self):
        from apps.media_library.quotas import StorageQuotaExceededError, enforce_upload_quotas

        org = _org()
        ws = _workspace(org=org)
        _subscribe(ws, base_bytes=500 * GB)  # generous workspace quota
        with override_settings(STORAGE_QUOTA_DEFAULT=1000, STORAGE_QUOTA_TIERS={}):
            _asset(ws, 900)
            with self.assertRaises(StorageQuotaExceededError):
                enforce_upload_quotas(org, ws, 500)

    def test_workspace_cap_blocks_even_with_org_headroom(self):
        from apps.media_library.quotas import enforce_upload_quotas

        org = _org()
        ws = _workspace(org=org)
        _subscribe(ws, base_bytes=1000)
        with override_settings(STORAGE_QUOTA_DEFAULT=500 * GB, STORAGE_QUOTA_TIERS={}):
            _asset(ws, 900)
            with self.assertRaises(WorkspaceStorageExceededError):
                enforce_upload_quotas(org, ws, 500)

    def test_passes_when_both_caps_have_room(self):
        from apps.media_library.quotas import enforce_upload_quotas

        org = _org()
        ws = _workspace(org=org)
        _subscribe(ws, base_bytes=10 * GB)
        with override_settings(STORAGE_QUOTA_DEFAULT=500 * GB, STORAGE_QUOTA_TIERS={}):
            _asset(ws, 1 * GB)
            enforce_upload_quotas(org, ws, 1 * GB)


@pytest.mark.django_db
class TestCreateAssetRespectsWorkspaceQuota:
    """The media_library service chokepoint honours the workspace cap."""

    def _png(self, size=64):
        # A real PNG header so ``validate_file``'s magic-byte sniff passes;
        # padded to the size the quota arithmetic needs.
        header = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        )
        return SimpleUploadedFile("x.png", header + b"\x00" * max(size - len(header), 0), content_type="image/png")

    def test_create_asset_blocked_when_workspace_is_full(self):
        from apps.media_library.services import create_asset

        cache.clear()
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 1000)

        with pytest.raises(WorkspaceStorageExceededError):
            create_asset(ws.organization, ws, self._png(200), None)

    def test_create_asset_allowed_with_headroom(self):
        from apps.media_library.services import create_asset

        cache.clear()
        ws = _workspace()
        _subscribe(ws, base_bytes=10 * GB)
        asset = create_asset(ws.organization, ws, self._png(200), None)
        assert asset.pk is not None
        assert asset.workspace_id == ws.pk

    def test_create_asset_unblocked_by_an_active_addon(self):
        from apps.media_library.services import create_asset

        cache.clear()
        ws = _workspace()
        _subscribe(ws, base_bytes=1000)
        _asset(ws, 1000)

        with pytest.raises(WorkspaceStorageExceededError):
            create_asset(ws.organization, ws, self._png(200), None)

        addon = services.contract_storage_addon(ws, 1)
        addon.status = WorkspaceAddon.Status.ACTIVE
        addon.save(update_fields=["status", "updated_at"])

        asset = create_asset(ws.organization, ws, self._png(200), None)
        assert asset.pk is not None
