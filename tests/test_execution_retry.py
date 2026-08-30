"""The durable boundary between recording an observation and acting on it.

Stage 1 (record_check) is the only thing that touches the network and the only
thing that creates a CheckResult. Stage 2 (process_recorded_result) reads a row
that already exists. These tests hold that line: when stage 2 fails, retrying it
must not re-probe the target site or write a second observation.
"""

import socket

import httpx
import pytest

from incidents.models import Incident, IncidentStatus
from monitors import ssrf
from monitors.checks import tls
from monitors.execution import execute_monitor, process_recorded_result, record_check
from monitors.models import CheckResult

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]

    monkeypatch.setattr(ssrf.socket, 'getaddrinfo', fake_getaddrinfo)
    monkeypatch.setattr(tls, 'inspect_certificate', lambda **kwargs: None)


@pytest.fixture
def network():
    """A transport that counts every request that reaches the wire."""

    class Network:
        def __init__(self):
            self.calls = 0
            self.status_code = 500

        def transport(self):
            def handler(request):
                self.calls += 1
                return httpx.Response(self.status_code)

            return httpx.MockTransport(handler)

    return Network()


@pytest.fixture
def broken_processing(monkeypatch):
    """Make the incident stage fail, as an internal bug would."""
    import monitors.execution as execution

    def explode(check_result):
        raise RuntimeError('incident engine blew up')

    monkeypatch.setattr(execution, 'process_check_result', explode)
    return monkeypatch


def test_retrying_the_incident_stage_does_not_repeat_the_request(
    monitor, network, broken_processing
):
    """The whole point: one probe, one observation, retried bookkeeping."""
    with pytest.raises(RuntimeError):
        execute_monitor(monitor, transport=network.transport())

    assert network.calls == 1
    result = CheckResult.objects.get()
    assert result.incident_processed_at is None
    assert not Incident.objects.exists()

    # The retry: same result id, no monitor, no transport.
    broken_processing.undo()
    process_recorded_result(result.pk)

    assert network.calls == 1
    assert CheckResult.objects.count() == 1
    result.refresh_from_db()
    assert result.incident_processed_at is not None


def test_the_observation_survives_a_failed_incident_stage_unaltered(
    monitor, network, broken_processing
):
    """An internal error is never written down as a fact about the target."""
    network.status_code = 200

    with pytest.raises(RuntimeError):
        execute_monitor(monitor, transport=network.transport())

    result = CheckResult.objects.get()
    assert result.is_success
    assert result.status_code == 200
    assert result.error_type is None
    assert result.error_message is None


def test_retry_reaches_the_same_state_as_an_uninterrupted_run(monitor, network, monkeypatch):
    import monitors.execution as execution

    execute_monitor(monitor, transport=network.transport())

    def explode(check_result):
        raise RuntimeError('incident engine blew up')

    monkeypatch.setattr(execution, 'process_check_result', explode)
    with pytest.raises(RuntimeError):
        execute_monitor(monitor, transport=network.transport())

    assert network.calls == 2
    assert CheckResult.objects.count() == 2
    assert not Incident.objects.exists()

    monkeypatch.undo()
    unprocessed = CheckResult.objects.get(incident_processed_at__isnull=True)
    process_recorded_result(unprocessed.pk)

    assert network.calls == 2
    assert CheckResult.objects.count() == 2
    incident = Incident.objects.get()
    assert incident.status == IncidentStatus.OPEN
    assert incident.failure_count == 2


def test_repeated_retries_are_harmless(monitor, network):
    execute_monitor(monitor, transport=network.transport())
    result = execute_monitor(monitor, transport=network.transport())

    for _ in range(5):
        process_recorded_result(result.pk)

    assert network.calls == 2
    assert CheckResult.objects.count() == 2
    incident = Incident.objects.get()
    assert incident.failure_count == 2
    assert incident.recovery_count == 0


def test_the_retry_stage_cannot_be_handed_a_monitor(monitor, network):
    """Stage 2 takes an id, so there is no argument that reaches the network."""
    with pytest.raises(Exception):  # noqa: B017 - any rejection proves the point
        process_recorded_result(monitor)

    assert network.calls == 0


def test_recording_a_check_does_not_touch_incident_state(monitor, network):
    """Stage 1 stands alone: an observation with nothing acted on yet."""
    result = record_check(monitor, transport=network.transport())

    assert network.calls == 1
    assert CheckResult.objects.count() == 1
    assert result.incident_processed_at is None
    assert not Incident.objects.exists()


def test_stage_two_is_what_creates_incidents(monitor, network):
    first = record_check(monitor, transport=network.transport())
    second = record_check(monitor, transport=network.transport())

    assert not Incident.objects.exists()

    process_recorded_result(first.pk)
    process_recorded_result(second.pk)

    assert network.calls == 2
    assert CheckResult.objects.count() == 2
    assert Incident.objects.get().failure_count == 2


def test_a_missing_result_id_fails_loudly(monitor):
    with pytest.raises(CheckResult.DoesNotExist):
        process_recorded_result(999999)
