"""R54-D05: creating an Iroh room retires the restore armed by an Iroh stop."""

import pytest
from test_r54_d01_restore_route import return_to_cloud
from test_r54_d01_restore_route import route as route  # pytest fixture


def stop_iroh_for_cloud(harness):
    # Iroh to Cloud arms a restore that waits for the stopped room's NOT CONNECTED.
    harness.controller.mode = harness.store.replication_mode = "iroh"
    harness.controller.replication_mode_changed("centralized")
    assert harness.controller.cloud_restore_after_iroh_stop


def test_room_creation_retires_pending_iroh_stop_restore(route):
    harness, cloud, state, stream = route
    stop_iroh_for_cloud(harness)
    harness.controller.create_iroh_room("route test")
    assert harness.controller.mode == "iroh"
    state.online = True
    harness.controller.iroh_status_changed("NOT CONNECTED")  # late, from the stopped room
    assert not harness.controller.cloud_restore_after_iroh_stop
    assert state.requests == state.users == [] and state.timers == []
    assert not cloud.authenticated
    stream.assert_not_called()


def test_cloud_return_after_room_creation_restores(route):
    harness, cloud, state, stream = route
    stop_iroh_for_cloud(harness)
    harness.controller.create_iroh_room("route test")
    harness.controller.iroh_status_changed("NOT CONNECTED")
    return_to_cloud(harness, cloud, state, stream)


@pytest.mark.parametrize("failure", [OSError("create failed"), ValueError("bad name")])
def test_failed_room_creation_keeps_pending_iroh_stop_restore(route, failure):
    harness, cloud, state, stream = route
    stop_iroh_for_cloud(harness)
    harness.store.failure = failure
    outcome = harness.controller.create_iroh_room("route test")
    harness.store.failure = None
    assert outcome.effects[0].error is failure
    assert harness.controller.mode == "centralized"
    state.online = True
    harness.controller.iroh_status_changed("NOT CONNECTED")
    assert cloud.authenticated and state.users == [{"id": "stored"}]
    stream.assert_called_once()
