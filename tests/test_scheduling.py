"""Scheduling: what is due, what gets claimed, and what happens next time.

No broker. The dispatcher and the run executor are plain functions, so
everything here drives them directly.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from monitors.models import Monitor, MonitorRun, MonitorType, RunStatus
from monitors.scheduling import (
    BROWSER_QUEUE,
    HTTP_QUEUE,
    claim_due_monitors,
    next_slot_after,
    queue_for,
)

pytestmark = pytest.mark.django_db


def due_monitor(website, minutes_overdue=1, **overrides):
    monitor = Monitor.objects.create(website=website, **overrides)
    past = timezone.now() - timedelta(minutes=minutes_overdue)
    Monitor.objects.filter(pk=monitor.pk).update(next_check_at=past)
    monitor.refresh_from_db()
    return monitor


class TestInitialScheduling:
    def test_a_new_enabled_monitor_is_due_immediately(self, website):
        """Users should see a result shortly after creating a monitor."""
        before = timezone.now()

        monitor = Monitor.objects.create(website=website)

        assert monitor.next_check_at is not None
        assert before <= monitor.next_check_at <= timezone.now()

    def test_creating_a_monitor_does_not_run_a_check(self, website):
        Monitor.objects.create(website=website)

        assert not MonitorRun.objects.exists()

    def test_a_new_disabled_monitor_is_not_scheduled(self, website):
        monitor = Monitor.objects.create(website=website, is_enabled=False)

        assert monitor.next_check_at is None

    def test_creating_through_the_api_schedules_without_running(self, auth_client, website):
        from django.urls import reverse

        response = auth_client.post(reverse('monitor-list'), {'website': website.pk}, format='json')

        assert response.status_code == 201
        assert response.json()['next_check_at'] is not None
        assert not MonitorRun.objects.exists()


class TestEnableDisableSemantics:
    def test_disabling_removes_it_from_the_schedule(self, monitor):
        monitor.is_enabled = False
        monitor.save()

        monitor.refresh_from_db()
        assert monitor.next_check_at is None

    def test_disabling_with_update_fields_still_unschedules(self, monitor):
        """A narrow save must not silently skip the scheduling rule."""
        monitor.is_enabled = False
        monitor.save(update_fields=['is_enabled'])

        monitor.refresh_from_db()
        assert monitor.next_check_at is None

    def test_re_enabling_schedules_it_again(self, monitor):
        monitor.is_enabled = False
        monitor.save()
        monitor.refresh_from_db()

        monitor.is_enabled = True
        monitor.save()

        monitor.refresh_from_db()
        assert monitor.next_check_at is not None
        assert monitor.next_check_at <= timezone.now()

    def test_an_unrelated_save_leaves_the_schedule_alone(self, monitor):
        original = monitor.next_check_at

        monitor.expected_text = 'unchanged schedule'
        monitor.save()

        monitor.refresh_from_db()
        assert monitor.next_check_at == original


class TestIntervalChanges:
    def test_changing_the_interval_reschedules_from_now(self, monitor):
        before = timezone.now()

        monitor.interval_seconds = 900
        monitor.save()

        monitor.refresh_from_db()
        expected = before + timedelta(seconds=900)
        assert monitor.next_check_at >= expected
        assert monitor.next_check_at <= timezone.now() + timedelta(seconds=900)

    def test_a_stale_schedule_cannot_survive_an_interval_change(self, website):
        monitor = due_monitor(website, minutes_overdue=600)

        monitor.interval_seconds = 3600
        monitor.save()

        monitor.refresh_from_db()
        assert monitor.next_check_at > timezone.now()

    def test_changing_the_interval_through_the_api_reschedules(self, auth_client, monitor):
        from django.urls import reverse

        response = auth_client.patch(
            reverse('monitor-detail', args=[monitor.pk]),
            {'interval_seconds': 600},
            format='json',
        )

        assert response.status_code == 200
        monitor.refresh_from_db()
        assert monitor.next_check_at > timezone.now()


class TestDispatch:
    def test_a_due_monitor_is_claimed(self, website):
        monitor = due_monitor(website)

        claimed = claim_due_monitors()

        assert len(claimed) == 1
        run, queue = claimed[0]
        assert run.monitor_id == monitor.pk
        assert run.status == RunStatus.PENDING
        assert queue == HTTP_QUEUE

    def test_the_run_records_the_slot_it_belongs_to(self, website):
        monitor = due_monitor(website)
        slot = monitor.next_check_at

        claim_due_monitors()

        assert MonitorRun.objects.get().scheduled_for == slot

    def test_a_monitor_that_is_not_due_is_ignored(self, monitor):
        Monitor.objects.filter(pk=monitor.pk).update(
            next_check_at=timezone.now() + timedelta(minutes=5)
        )

        assert claim_due_monitors() == []
        assert not MonitorRun.objects.exists()

    def test_a_disabled_monitor_is_ignored(self, website):
        monitor = due_monitor(website)
        Monitor.objects.filter(pk=monitor.pk).update(is_enabled=False)

        assert claim_due_monitors() == []

    def test_an_unscheduled_monitor_is_ignored(self, website):
        monitor = due_monitor(website)
        Monitor.objects.filter(pk=monitor.pk).update(next_check_at=None)

        assert claim_due_monitors() == []

    def test_the_next_due_time_moves_into_the_future(self, website):
        monitor = due_monitor(website)

        claim_due_monitors()

        monitor.refresh_from_db()
        assert monitor.next_check_at > timezone.now()
        assert monitor.last_scheduled_at is not None

    def test_a_second_pass_claims_nothing_more(self, website):
        due_monitor(website)

        claim_due_monitors()
        second = claim_due_monitors()

        assert second == []
        assert MonitorRun.objects.count() == 1

    def test_several_monitors_are_claimed_in_one_pass(self, website):
        for _ in range(5):
            due_monitor(website)

        claimed = claim_due_monitors()

        assert len(claimed) == 5
        assert MonitorRun.objects.count() == 5

    def test_the_batch_size_bounds_one_pass(self, website):
        for _ in range(6):
            due_monitor(website)

        claimed = claim_due_monitors(limit=4)

        assert len(claimed) == 4
        assert MonitorRun.objects.count() == 4

    def test_a_deleted_monitor_takes_its_runs_with_it(self, website):
        monitor = due_monitor(website)
        claim_due_monitors()

        monitor.delete()

        assert not MonitorRun.objects.exists()


class TestCoalescing:
    """Being offline for hours must not produce hours of backlog."""

    def test_a_badly_overdue_monitor_produces_one_run(self, website):
        due_monitor(website, minutes_overdue=600, interval_seconds=60)

        claimed = claim_due_monitors()

        assert len(claimed) == 1
        assert MonitorRun.objects.count() == 1

    def test_the_missed_slots_are_skipped_in_one_step(self, website):
        monitor = due_monitor(website, minutes_overdue=600, interval_seconds=60)

        claim_due_monitors()

        monitor.refresh_from_db()
        assert monitor.next_check_at > timezone.now()
        # And not merely one interval past the stale slot.
        assert monitor.next_check_at <= timezone.now() + timedelta(seconds=60)

    def test_repeated_dispatches_never_accumulate_a_backlog(self, website):
        due_monitor(website, minutes_overdue=1440, interval_seconds=60)

        for _ in range(5):
            claim_due_monitors()

        assert MonitorRun.objects.count() == 1


class TestNextSlot:
    def test_a_future_slot_advances_by_one_interval(self):
        now = timezone.now()
        slot = now + timedelta(seconds=30)

        assert next_slot_after(slot, 60, now) == slot + timedelta(seconds=60)

    def test_an_overdue_slot_lands_strictly_in_the_future(self):
        now = timezone.now()
        slot = now - timedelta(hours=10)

        assert next_slot_after(slot, 60, now) > now

    def test_the_result_stays_on_the_original_cadence(self):
        now = timezone.now()
        slot = now - timedelta(seconds=125)

        advanced = next_slot_after(slot, 60, now)

        assert (advanced - slot).total_seconds() % 60 == 0

    def test_a_slot_exactly_now_moves_forward(self):
        now = timezone.now()

        assert next_slot_after(now, 60, now) > now


class TestQueueRouting:
    @pytest.mark.parametrize(
        ('monitor_type', 'expected'),
        [
            (MonitorType.HTTP, HTTP_QUEUE),
            (MonitorType.BROWSER, BROWSER_QUEUE),
            (MonitorType.FLOW, BROWSER_QUEUE),
        ],
    )
    def test_work_is_routed_by_monitor_type(self, monitor_type, expected):
        assert queue_for(monitor_type) == expected

    def test_the_dispatcher_routes_a_browser_monitor(self, website):
        due_monitor(website, monitor_type=MonitorType.BROWSER)

        ((_, queue),) = claim_due_monitors()

        assert queue == BROWSER_QUEUE

    def test_the_dispatcher_routes_a_flow_monitor(self, website):
        due_monitor(website, monitor_type=MonitorType.FLOW)

        ((_, queue),) = claim_due_monitors()

        assert queue == BROWSER_QUEUE

    def test_the_dispatcher_routes_an_http_monitor(self, website):
        due_monitor(website, monitor_type=MonitorType.HTTP)

        ((_, queue),) = claim_due_monitors()

        assert queue == HTTP_QUEUE


class TestDeferredLoads:
    """The schedule hook must survive partially-loaded instances.

    Django selects only the primary key when collecting rows for a cascade
    delete. Reading a field that was not selected would refetch the row, which
    re-enters the hook; this is the regression that guards against it.
    """

    def test_a_deferred_load_does_not_recurse(self, monitor):
        loaded = Monitor.objects.only('pk').get(pk=monitor.pk)

        assert loaded.pk == monitor.pk

    def test_a_deferred_field_can_still_be_read(self, monitor):
        loaded = Monitor.objects.only('pk').get(pk=monitor.pk)

        assert loaded.is_enabled is True

    def test_cascading_from_the_owner_deletes_cleanly(self, website, user):
        Monitor.objects.create(website=website)

        user.delete()

        assert not Monitor.objects.exists()

    def test_deleting_a_website_cascades_to_monitors(self, website):
        Monitor.objects.create(website=website)

        website.delete()

        assert not Monitor.objects.exists()

    def test_a_deferred_instance_saves_without_disturbing_the_schedule(self, monitor):
        original = monitor.next_check_at

        loaded = Monitor.objects.defer('expected_text').get(pk=monitor.pk)
        loaded.save()

        loaded.refresh_from_db()
        assert loaded.next_check_at == original
