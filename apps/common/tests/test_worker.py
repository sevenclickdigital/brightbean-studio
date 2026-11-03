"""Publishing gets a slot even when the shared background queue is blocked."""

import signal
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import pytest
from background_task.models import CompletedTask, Task
from background_task.tasks import Tasks
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.utils import timezone

from apps.common.worker import PUBLISHING_TASK_NAMES, SHUTDOWN_REQUESTED, StudioWorker

pytestmark = pytest.mark.django_db(transaction=True)
PUBLISH = "apps.publisher.tasks.run_publish_cycle"
CONFIRM = "apps.publisher.tasks.confirm_pending_publishes"


@pytest.fixture
def registry():
    Task.objects.all().delete()
    CompletedTask.objects.all().delete()
    return Tasks()


def queue(registry, name, fn, **options):
    proxy = registry.background(name=name)(fn)
    return proxy(**options)


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("Worker did not reach the expected state within five seconds")


@contextmanager
def running(worker, *release_events):
    errors = []

    def drive():
        try:
            worker.run()
        except Exception as exc:
            errors.append(exc)

    driver = threading.Thread(target=drive, daemon=True)
    driver.start()
    try:
        yield driver
    finally:
        worker.request_stop()
        for event in release_events:
            event.set()
        driver.join(5)
        assert not driver.is_alive(), "Worker did not drain its released tasks"
        assert not errors


@pytest.mark.parametrize("legacy_async", [False, True])
def test_publishing_and_confirmation_run_during_blocked_inbox_and_large_backlog(
    registry, settings, legacy_async, caplog
):
    settings.BACKGROUND_TASK_RUN_ASYNC = legacy_async
    settings.BACKGROUND_TASK_ASYNC_THREADS = 1
    started = threading.Event()
    release = threading.Event()
    published = threading.Event()
    confirmed = threading.Event()

    def inbox():
        started.set()
        assert release.wait(10)

    queue(registry, "test.inbox", inbox, priority=10)
    registry.background(name="test.maintenance")(lambda: None)
    Task.objects.bulk_create([Task.objects.new_task("test.maintenance") for _ in range(2000)])
    worker = StudioWorker(registry=registry, poll_interval=0.01)
    with running(worker, release), caplog.at_level("INFO", logger="apps.common.worker"):
        assert started.wait(5)
        queue(registry, PUBLISH, published.set)
        queue(registry, CONFIRM, confirmed.set)
        assert published.wait(5)
        assert confirmed.wait(5)
        assert not release.is_set()
        assert Task.objects.filter(task_name="test.maintenance", locked_at__isnull=False).count() == 0
    assert "dispatch_delay_seconds=" in caplog.text
    assert "duration_seconds=" in caplog.text
    assert "id=None" not in caplog.text


def test_two_publish_cycles_never_overlap(registry):
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    calls = []
    live = peak = 0
    guard = threading.Lock()

    def publish():
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
            calls.append(1)
            (first_started if len(calls) == 1 else second_started).set()
        try:
            assert release.wait(10)
        finally:
            with guard:
                live -= 1

    queue(registry, PUBLISH, publish)
    queue(registry, PUBLISH, publish)
    worker = StudioWorker(registry=registry, poll_interval=0.01)
    with running(worker, release):
        assert first_started.wait(5)
        assert Task.objects.filter(task_name=PUBLISH, locked_at__isnull=False).count() == 1
        assert not second_started.wait(0.1)
        release.set()
        assert second_started.wait(5)
    assert peak == 1


def test_success_uses_library_completion_and_repetition(registry):
    original = queue(registry, PUBLISH, lambda: None, repeat=15, verbose_name="publisher")
    worker = StudioWorker(registry=registry, poll_interval=0.01)
    with running(worker):
        wait_for(lambda: CompletedTask.objects.filter(task_name=PUBLISH).exists())
    assert not Task.objects.filter(pk=original.pk).exists()
    successor = Task.objects.get(task_name=PUBLISH)
    assert successor.repeat == 15
    assert successor.verbose_name == "publisher"
    assert successor.run_at > timezone.now()
    assert successor.locked_at is None


@pytest.mark.parametrize("fail", [False, True])
def test_task_threads_close_connections_on_success_and_failure(registry, fail):
    captured = []

    def work():
        Task.objects.count()
        captured.append(connections["default"])
        if fail:
            raise RuntimeError("provider unavailable")

    original = queue(registry, "test.work", work)
    worker = StudioWorker(registry=registry, poll_interval=0.01)
    with running(worker):
        if fail:
            wait_for(lambda: Task.objects.filter(pk=original.pk, attempts=1, locked_at__isnull=True).exists())
        else:
            wait_for(lambda: CompletedTask.objects.filter(task_name="test.work").exists())
    assert len(captured) == 1
    assert captured[0].connection is None
    if fail:
        original.refresh_from_db()
        assert original.locked_by is None
        assert original.run_at > timezone.now()
        assert "provider unavailable" in original.last_error


def test_runner_failure_preserves_lock_for_stale_recovery(registry):
    original = queue(registry, "test.work", lambda: None)
    worker = StudioWorker(registry=registry, poll_interval=0.01)
    with patch("apps.common.worker.bg_runner", side_effect=RuntimeError("result write failed")), running(worker):
        wait_for(lambda: Task.objects.filter(pk=original.pk, locked_at__isnull=False).exists())
    original.refresh_from_db()
    assert original.locked_by is not None
    assert original.locked_at is not None
    assert original.attempts == 0


def test_claim_respects_eligibility_registration_priority_and_stale_locks(registry):
    registry.background(name="test.work")(lambda: None)
    now = timezone.now()
    for options in (
        {"run_at": now + timedelta(hours=1)},
        {"failed_at": now},
        {"locked_at": now, "locked_by": "other"},
    ):
        task = Task.objects.new_task("test.work", priority=50)
        for field, value in options.items():
            setattr(task, field, value)
        task.save()
    Task.objects.new_task("test.unknown", priority=100).save()
    eligible = Task.objects.new_task("test.work", priority=10)
    eligible.save()
    stale = Task.objects.new_task("test.work", priority=5)
    stale.locked_at = now - timedelta(hours=2)
    stale.locked_by = "old-process"
    stale.save()
    worker = StudioWorker(registry=registry)
    assert worker._claim("general").pk == eligible.pk
    assert worker._claim("general").pk == stale.pk
    assert worker._claim("general") is None
    assert worker._claim("publishing") is None


def test_recycling_keeps_publishing_until_general_work_drains(registry):
    background_started = threading.Event()
    background_release = threading.Event()
    publish_started = threading.Event()
    publish_release = threading.Event()
    next_general = threading.Event()
    next_publish = threading.Event()

    def background():
        background_started.set()
        assert background_release.wait(10)

    def publish():
        publish_started.set()
        assert publish_release.wait(10)

    queue(registry, "test.inbox", background, priority=10)
    queue(registry, "test.next_general", next_general.set)
    worker = StudioWorker(registry=registry, duration=0.2, poll_interval=0.01)
    with running(worker, background_release, publish_release) as driver:
        assert background_started.wait(5)
        wait_for(lambda: worker.recycling)
        queue(registry, PUBLISH, publish)
        assert publish_started.wait(5)
        assert not next_general.is_set()
        background_release.set()
        wait_for(lambda: "general" not in worker.active)
        queue(registry, CONFIRM, next_publish.set)
        publish_release.set()
        driver.join(5)
        assert not driver.is_alive()
        assert not next_general.is_set()
        assert not next_publish.is_set()


def test_stop_prevents_new_claims_and_does_not_unlock_running_jobs(registry):
    started = {name: threading.Event() for name in (PUBLISH, "test.inbox")}
    release = threading.Event()

    def work(name):
        started[name].set()
        assert release.wait(10)

    for name in started:
        queue(registry, name, lambda name=name: work(name))
    worker = StudioWorker(registry=registry, poll_interval=0.01)
    with running(worker, release):
        assert all(event.wait(5) for event in started.values())
        worker.request_stop()
        assert Task.objects.filter(locked_at__isnull=False).count() == 2
        queue(registry, CONFIRM, lambda: pytest.fail("Claimed after termination"))
        queue(registry, "test.later", lambda: pytest.fail("Claimed after termination"))
    assert Task.objects.filter(task_name__in=[CONFIRM, "test.later"], locked_at__isnull=True).count() == 2


def test_command_installs_and_restores_termination_handlers(registry):
    before = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}

    def terminate(worker):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert worker.stop_requested.is_set()
        # In-flight publishes read this to cut long waits short.
        assert SHUTDOWN_REQUESTED.is_set()

    with patch.object(StudioWorker, "run", autospec=True, side_effect=terminate):
        call_command("run_studio_worker", duration=3600)
    assert {signum: signal.getsignal(signum) for signum in before} == before
    assert not SHUTDOWN_REQUESTED.is_set()
    from background_task.tasks import tasks

    assert "apps.publisher.engine._post_first_comment_task" in tasks._tasks
    assert PUBLISHING_TASK_NAMES.issubset(tasks._tasks)


@pytest.mark.parametrize("options", [{"duration": -1}, {"sleep": 0}, {"sleep": -1}])
def test_command_rejects_invalid_intervals(registry, options):
    with pytest.raises(CommandError):
        call_command("run_studio_worker", **options)


# ---------------------------------------------------------------------------
# Stale lock recovery
# ---------------------------------------------------------------------------
#
# A task killed mid-run keeps its lock row, and ``Task.objects.unlocked()``
# hides it until MAX_RUN_TIME (3600s by default) expires. One badly-timed
# deploy therefore parks a five-minute job for a full hour — which is how the
# inbox sync twice stopped ingesting comments while the worker looked healthy.


def test_startup_releases_locks_left_by_a_previous_process(registry):
    """The corpse of the container we replaced is cleared, so the task runs again."""
    worker = StudioWorker(registry=registry)
    task = queue(registry, "apps.inbox.tasks.run_inbox_sync_cycle", lambda: None)
    # Exactly the state a SIGKILL mid-run leaves behind: locked under the name
    # this process also uses, because the container entrypoint is always PID 1.
    Task.objects.filter(pk=task.pk).update(locked_by=worker.worker_name, locked_at=timezone.now())

    assert worker.release_own_stale_locks() == 1

    task.refresh_from_db()
    assert task.locked_by is None
    assert task.locked_at is None
    # The point of the unlock: the task is claimable again rather than waiting
    # out MAX_RUN_TIME.
    assert Task.objects.unlocked(timezone.now()).filter(pk=task.pk).exists()


def test_startup_leaves_another_worker_s_locks_alone(registry):
    """A peer's lock is not ours to reclaim — it may still be running.

    This is the half that keeps the unlock safe. The same worker runs the
    publish cycle, where unlocking a task that is genuinely in flight risks a
    second post going out.
    """
    worker = StudioWorker(registry=registry)
    task = queue(registry, PUBLISH, lambda: None)
    locked_at = timezone.now()
    Task.objects.filter(pk=task.pk).update(locked_by=f"other-{worker.worker_name}", locked_at=locked_at)

    assert worker.release_own_stale_locks() == 0

    task.refresh_from_db()
    assert task.locked_by == f"other-{worker.worker_name}"
    assert task.locked_at == locked_at


def test_startup_does_not_touch_unlocked_tasks(registry):
    """An ordinary queued task is left exactly as it was."""
    worker = StudioWorker(registry=registry)
    task = queue(registry, "apps.inbox.tasks.run_inbox_sync_cycle", lambda: None)

    assert worker.release_own_stale_locks() == 0

    task.refresh_from_db()
    assert task.locked_by is None
