"""The client state machine against a fake server over a real Unix socket or,
on Windows, a named pipe."""

import shutil
import struct
import sys
import threading
import time

import pytest

from comapeo_qgis.core.discovery import find_servers
from comapeo_qgis.core.model import FeaturePage, parse_feature_page, parse_layers
from comapeo_qgis.core.protocol import (
    UPDATE_APP,
    UPDATE_PLUGIN,
    Authenticated,
    Backoff,
    Closed,
    HandshakeRejected,
    Notification,
    ProtocolViolation,
    VersionMismatch,
)
from comapeo_qgis.core.reconcile import Reconciler, SyncPlan
from tests.driver import BlockingClient, RemoteError
from tests.fake_server import (
    FakeProject,
    FakeServer,
    is_pipe,
    listen,
    new_endpoint,
    observation,
    short_tempdir,
    track,
)

_open_clients: list[BlockingClient] = []


@pytest.fixture(autouse=True)
def close_clients():
    yield
    while _open_clients:
        _open_clients.pop().close()


@pytest.fixture
def server():
    srv = FakeServer()
    project = srv.add_project(FakeProject("proj-1", "Rio Negro"))
    project.features["observations"] = [observation(f"obs-{i}") for i in range(7)]
    project.features["tracks"] = [track("trk-1")]
    with srv:
        yield srv


def client_for(socket_path, secret, **kwargs) -> BlockingClient:
    client = BlockingClient(socket_path, secret, **kwargs)
    _open_clients.append(client)
    return client


def connect(server, **kwargs) -> BlockingClient:
    client = client_for(server.socket_path, server.secret, **kwargs)
    event = client.connect()
    assert isinstance(event, Authenticated), event
    return client


def apply(cache: dict, plan: SyncPlan) -> None:
    for feature in plan.upserts:
        cache[feature.id] = feature.version
    for feature_id in plan.deletes:
        cache.pop(feature_id, None)


def server_versions(server, layer_id="observations") -> dict:
    features = server.projects["proj-1"].features.get(layer_id, [])
    return {f["id"]: f["version"] for f in features}


def get_page(client, layer, cursor=None, limit=3) -> FeaturePage:
    params = {"projectId": "proj-1", "layerId": layer.layer_id, "limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    return parse_feature_page(client.call("layers.getFeatures", params), layer)


def observations_layer(client):
    layers = parse_layers(client.call("layers.list", {"projectId": "proj-1"}))
    return next(layer for layer in layers if layer.layer_id == "observations")


def fetch_all(client, layer, ids=None, limit=3):
    pages = []
    cursor = None
    while True:
        params = {"projectId": "proj-1", "layerId": layer.layer_id, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if ids is not None:
            params["ids"] = ids
        page = parse_feature_page(client.call("layers.getFeatures", params), layer)
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return pages


def test_authenticates_and_lists_projects(server):
    client = connect(server)
    assert client.conn.app_info == {"name": "CoMapeo Desktop", "version": "9.9.9"}
    result = client.call("projects.list")
    assert result["projects"] == [{"projectId": "proj-1", "name": "Rio Negro"}]
    client.close()


def test_wrong_secret_server_closes_without_a_word(server):
    client = client_for(server.socket_path, "wrong-secret")
    event = client.connect()
    assert isinstance(event, HandshakeRejected)
    later = [e for e in client.log if isinstance(e, Closed)]
    assert later == [Closed(was_authenticated=False)]
    assert server.requests[0]["method"] == "hello"
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("server_versions", "client_versions", "update"),
    [((1,), (2,), UPDATE_APP), ((2, 3), (1,), UPDATE_PLUGIN)],
)
def test_version_mismatch_says_which_side_to_update(
    server_versions, client_versions, update
):
    with FakeServer(protocol_versions=server_versions) as srv:
        client = client_for(
            srv.socket_path, srv.secret, protocol_versions=client_versions
        )
        event = client.connect()
    assert isinstance(event, VersionMismatch)
    assert event.update == update
    assert event.server_versions == server_versions


def test_unknown_method_and_project_errors(server):
    client = connect(server)
    with pytest.raises(RemoteError) as info:
        client.call("features.create", {})
    assert info.value.code == "METHOD_NOT_FOUND"
    with pytest.raises(RemoteError) as info:
        client.call("layers.list", {"projectId": "nope"})
    assert info.value.code == "PROJECT_NOT_FOUND"


def test_full_fetch_across_pages_then_reconcile(server):
    client = connect(server)
    layers = parse_layers(client.call("layers.list", {"projectId": "proj-1"}))
    observations = next(layer for layer in layers if layer.layer_id == "observations")
    cached = {"obs-0": "v1", "obs-1": "old", "gone": "v1"}
    reconciler = Reconciler(cached, refresh_all=False)
    upserted, deleted = [], []
    pages = fetch_all(client, observations, limit=3)
    assert [len(p.features) for p in pages] == [3, 3, 1]
    for page in pages:
        plan = reconciler.add_page(page)
        assert plan.deletes == ()
        upserted += [f.id for f in plan.upserts]
    deleted += reconciler.finish().deletes
    assert sorted(upserted) == [f"obs-{i}" for i in range(1, 7)]
    assert deleted == ["gone"]


def test_invalid_cursor(server):
    client = connect(server)
    with pytest.raises(RemoteError) as info:
        client.call(
            "layers.getFeatures",
            {"projectId": "proj-1", "layerId": "observations", "cursor": "zzz"},
        )
    assert info.value.code == "INVALID_CURSOR"


def test_subscribe_notification_then_fetch_ids_with_removed(server):
    client = connect(server)
    layers = parse_layers(client.call("layers.list", {"projectId": "proj-1"}))
    observations = next(layer for layer in layers if layer.layer_id == "observations")
    assert client.call("subscribe", {"projectId": "proj-1"}) == {}

    project = server.projects["proj-1"]
    project.features["observations"] = [
        f for f in project.features["observations"] if f["id"] != "obs-2"
    ]
    project.features["observations"][0] = observation("obs-0", "v2")
    server.notify(
        "layer.changed",
        {"projectId": "proj-1", "layerId": "observations", "ids": ["obs-0", "obs-2"]},
    )
    note = client.wait_for(lambda e: isinstance(e, Notification))
    assert note.method == "layer.changed"
    ids = note.params["ids"]

    cached = {f"obs-{i}": "v1" for i in range(7)}
    reconciler = Reconciler(cached, ids=ids)
    plans = [reconciler.add_page(p) for p in fetch_all(client, observations, ids)]
    plans.append(reconciler.finish())
    assert [f.id for p in plans for f in p.upserts] == ["obs-0"]
    assert [d for p in plans for d in p.deletes] == ["obs-2"]


def test_no_notifications_after_unsubscribe(server):
    client = connect(server)
    client.call("subscribe", {"projectId": "proj-1"})
    client.call("unsubscribe", {"projectId": "proj-1"})
    server.notify("layer.schemaChanged", {"projectId": "proj-1", "layerId": "x"})
    client.call("projects.list")
    assert not any(isinstance(e, Notification) for e in client.log)


def test_frames_written_one_byte_at_a_time(server):
    server.chunk_size = 1
    client = connect(server)
    result = client.call("layers.list", {"projectId": "proj-1"})
    assert [layer["layerId"] for layer in result["layers"]] == [
        "observations",
        "tracks",
    ]


def test_oversize_frame_from_server_is_a_protocol_violation(server):
    client = connect(server)
    server.broadcast_raw(struct.pack("<I", 16 * 1024 * 1024 + 1))
    event = client.wait_for(lambda e: isinstance(e, ProtocolViolation))
    assert "exceeds" in event.message
    assert client.sock is None


def test_server_close_then_reconnect_with_backoff():
    shared_dir = short_tempdir()
    shared_path = new_endpoint(shared_dir)
    server = FakeServer(socket_path=shared_path).start()
    server.add_project(FakeProject("proj-1", "Rio Negro"))
    client = connect(server)
    client.call("subscribe", {"projectId": "proj-1"})
    server.drop_connections()
    closed = client.wait_for(lambda e: isinstance(e, Closed))
    assert closed.was_authenticated

    server.stop()
    backoff = Backoff(initial=0.01, maximum=0.05)
    attempts = 0
    restarted = FakeServer(socket_path=shared_path, secret=server.secret)
    restarted.add_project(server.projects["proj-1"])
    while True:
        attempts += 1
        if attempts == 3:
            restarted.start()
        try:
            again = connect(restarted)
            break
        except (ConnectionRefusedError, FileNotFoundError):
            time.sleep(backoff.next_delay())
    try:
        assert attempts == 3
        assert again.call("projects.list")["projects"][0]["projectId"] == "proj-1"
    finally:
        again.close()
        restarted.stop()
        shutil.rmtree(shared_dir, ignore_errors=True)


def test_the_fake_server_listens_on_a_named_pipe_on_windows(server):
    assert is_pipe(server.socket_path) == (sys.platform == "win32")
    assert isinstance(connect(server).call("projects.list"), dict)


def test_discovery_file_leads_to_a_working_connection(server):
    path = server.write_discovery()
    [result] = find_servers(override=path)
    assert result.usable
    assert result.info.app_name == "CoMapeo Desktop"
    client = client_for(result.info.socket_path, result.info.secret)
    assert isinstance(client.connect(), Authenticated)


def test_discovery_marks_a_dead_socket_stale(server):
    directory = short_tempdir()
    path = server.write_discovery(directory)
    server.stop()
    try:
        [result] = find_servers(override=path)
    finally:
        shutil.rmtree(directory)
    assert result.stale and not result.usable


def test_restart_with_a_new_secret_and_socket_is_found_by_rereading_discovery():
    discovery_dir = short_tempdir()
    first = FakeServer().start()
    second = FakeServer()
    try:
        first.add_project(FakeProject("proj-1", "Rio Negro"))
        first.write_discovery(discovery_dir)
        [found] = find_servers(override=discovery_dir)
        client = client_for(found.info.socket_path, found.info.secret)
        assert isinstance(client.connect(), Authenticated)

        first.stop()
        client.wait_for(lambda e: isinstance(e, Closed))
        [stale] = find_servers(override=discovery_dir)
        assert stale.stale

        second.add_project(FakeProject("proj-1", "Rio Negro"))
        second.start().write_discovery(discovery_dir)
        [again] = find_servers(override=discovery_dir)
        assert again.usable
        assert again.info.socket_path != found.info.socket_path
        assert again.info.secret != found.info.secret
        old = client_for(again.info.socket_path, found.info.secret)
        assert isinstance(old.connect(), HandshakeRejected)
        fresh = client_for(again.info.socket_path, again.info.secret)
        assert isinstance(fresh.connect(), Authenticated)
        assert fresh.call("projects.list")["projects"][0]["projectId"] == "proj-1"
    finally:
        first.stop()
        second.stop()
        shutil.rmtree(discovery_dir, ignore_errors=True)


def test_a_server_that_accepts_but_never_answers_hello_times_out():
    directory = short_tempdir()
    path = new_endpoint(directory)
    listener = listen(path)
    listener.settimeout(5)
    accepted = []
    thread = threading.Thread(target=lambda: accepted.append(listener.accept()[0]))
    thread.start()
    try:
        client = client_for(path, "secret", handshake_timeout=0.3)
        started = time.monotonic()
        event = client.connect()
        assert isinstance(event, HandshakeRejected)
        assert "timeout" in event.reason
        assert time.monotonic() - started < 3
        assert client.sock is None
    finally:
        thread.join(timeout=2)
        for sock in accepted:
            sock.close()
        listener.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_invalid_cursor_mid_fetch_then_a_fresh_fetch_matches_the_server(server):
    client = connect(server)
    layer = observations_layer(client)
    cache = {"obs-0": "v1", "obs-5": "v1", "gone": "v1"}
    reconciler = Reconciler(cache)
    first = get_page(client, layer)
    apply(cache, reconciler.add_page(first))

    project = server.projects["proj-1"]
    project.features["observations"] = [
        f for f in project.features["observations"] if f["id"] != "obs-5"
    ] + [observation("obs-new", "v1")]
    server.cursor_epoch += 1
    with pytest.raises(RemoteError) as info:
        get_page(client, layer, cursor=first.next_cursor)
    assert info.value.code == "INVALID_CURSOR"

    reconciler = Reconciler(cache)
    cursor = None
    while True:
        page = get_page(client, layer, cursor=cursor)
        apply(cache, reconciler.add_page(page))
        cursor = page.next_cursor
        if cursor is None:
            break
    apply(cache, reconciler.finish())
    assert cache == server_versions(server)


def test_connection_lost_mid_fetch_then_reconnect_and_fetch_again(server):
    client = connect(server)
    layer = observations_layer(client)
    cache = {"gone": "v1"}
    apply(cache, Reconciler(cache).add_page(get_page(client, layer)))
    assert len(cache) == 4

    server.drop_connections()
    closed = client.wait_for(lambda e: isinstance(e, Closed))
    assert closed.was_authenticated

    again = connect(server)
    reconciler = Reconciler(cache)
    cursor = None
    while True:
        page = get_page(again, layer, cursor=cursor)
        apply(cache, reconciler.add_page(page))
        cursor = page.next_cursor
        if cursor is None:
            break
    apply(cache, reconciler.finish())
    assert cache == server_versions(server)


def test_an_empty_project_end_to_end(server):
    server.add_project(FakeProject("empty", "Nothing yet"))
    client = connect(server)
    layers = parse_layers(client.call("layers.list", {"projectId": "empty"}))
    assert [layer.layer_id for layer in layers] == ["observations", "tracks"]
    for layer in layers:
        result = client.call(
            "layers.getFeatures", {"projectId": "empty", "layerId": layer.layer_id}
        )
        page = parse_feature_page(result, layer)
        assert page.valid and page.is_last and page.features == ()
        cache = {"left-over": "v1"}
        reconciler = Reconciler(cache)
        apply(cache, reconciler.add_page(page))
        apply(cache, reconciler.finish())
        assert cache == {}


def test_track_times_from_the_server_become_m_values(server):
    client = connect(server)
    layers = parse_layers(client.call("layers.list", {"projectId": "proj-1"}))
    tracks = next(layer for layer in layers if layer.layer_id == "tracks")
    [page] = fetch_all(client, tracks)
    [record] = page.features
    assert record.geometry.has_m
    assert record.geometry.wkt().startswith("LineStringM (")
