"""The core against the real `@comapeo/local-api` dev server, when it is at hand.

Starts `node src/dev/cli.ts` from a sibling checkout (or COMAPEO_LOCAL_API_DIR)
and skips when that checkout, its dependencies or Node 22.18+ are missing.
Deselect with `-m "not real_server"`.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from comapeo_qgis.core.discovery import find_servers
from comapeo_qgis.core.media import MediaCache, MediaState
from comapeo_qgis.core.model import (
    parse_categories,
    parse_feature_page,
    parse_layer_changed,
    parse_layers,
    parse_projects,
)
from comapeo_qgis.core.protocol import Authenticated, HandshakeRejected, Notification
from comapeo_qgis.core.reconcile import Reconciler
from tests import dev_server as dev
from tests.driver import BlockingClient

pytestmark = pytest.mark.real_server

OBSERVATIONS = 25
TRACKS = 3


@pytest.fixture(scope="module")
def dev_server():
    reason = dev.skip_reason()
    if reason:
        pytest.skip(reason)
    server = dev.start(observations=OBSERVATIONS, tracks=TRACKS, photos=4, projects=1)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def clients():
    opened: list[BlockingClient] = []

    def make(info, secret=None, **kwargs) -> BlockingClient:
        client = BlockingClient(
            info.socket_path, secret or info.secret, request_timeout=30, **kwargs
        )
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.close()


@pytest.fixture
def session(dev_server, clients):
    if dev_server.proc.poll() is not None:
        pytest.fail(
            f"dev server exited ({dev_server.proc.returncode}):\n{dev_server.log()}"
        )
    [found] = find_servers(override=dev_server.discovery_dir)
    assert found.usable, found
    client = clients(found.info)
    assert isinstance(client.connect(), Authenticated)
    [project] = parse_projects(client.call("projects.list"))
    layers = {
        layer.layer_id: layer
        for layer in parse_layers(
            client.call("layers.list", {"projectId": project.project_id})
        )
    }
    return client, project.project_id, layers


def fetch_all(client, project_id, layer, limit=1000, ids=None):
    pages, cursor = [], None
    while True:
        params = {"projectId": project_id, "layerId": layer.layer_id, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if ids is not None:
            params["ids"] = ids
        page = parse_feature_page(client.call("layers.getFeatures", params), layer)
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return pages


def test_discovery_and_handshake(dev_server, clients):
    [found] = find_servers(override=dev_server.discovery_dir)
    assert found.usable
    assert len(found.info.socket_path.encode()) < 100
    assert isinstance(clients(found.info, secret="wrong").connect(), HandshakeRejected)
    client = clients(found.info)
    assert isinstance(client.connect(), Authenticated)
    assert client.conn.protocol_version == 1


def test_observations_page_through_and_reconcile(session):
    client, project_id, layers = session
    observations = layers["observations"]
    assert observations.geometry_type == "Point"
    pages = fetch_all(client, project_id, observations, limit=10)
    assert len(pages) >= 3
    assert all(p.valid and p.skipped == 0 for p in pages)
    ids = [f.id for p in pages for f in p.features]
    assert len(ids) == len(set(ids)) >= OBSERVATIONS
    assert any(f.geometry is None for p in pages for f in p.features)
    plan_ids = set()
    reconciler = Reconciler({"not-on-server": "v1"})
    for page in pages:
        plan_ids |= {f.id for f in reconciler.add_page(page).upserts}
    assert plan_ids == set(ids)
    assert reconciler.finish().deletes == ("not-on-server",)


def test_track_times_become_m_values(session):
    client, project_id, layers = session
    tracks = layers["tracks"]
    [page] = fetch_all(client, project_id, tracks)
    assert len(page.features) == TRACKS
    for record in page.features:
        assert record.geometry is not None and record.geometry.has_m, record.id
        assert record.geometry.wkt().startswith("LineString")
        m = record.geometry.m_values
        assert len(m) == len(record.geometry.coordinates)
        assert all(1e9 < value < 1e10 for value in m)


def test_categories_have_usable_icons(session):
    client, project_id, _layers = session
    categories = parse_categories(
        client.call("categories.list", {"projectId": project_id})
    )
    assert categories
    assert all(c.icon is None or c.icon.data for c in categories)


def test_live_delete_arrives_as_layer_changed_then_ids_fetch_removes_it(
    dev_server, session
):
    client, project_id, layers = session
    observations = layers["observations"]
    [first] = fetch_all(client, project_id, observations)
    # Leave the media test its seeded attachments, including the missing one.
    plain = [f for f in first.features if not f.attachments]
    victim, survivor = plain[0].id, plain[1].id
    survivor_version = plain[1].version
    assert client.call("subscribe", {"projectId": project_id}) == {}

    dev_server.command(f"delete 1 {victim}")
    note = client.wait_for(
        lambda e: isinstance(e, Notification) and e.method == "layer.changed",
        timeout=10,
    )
    changed = parse_layer_changed(note.params)
    assert changed is not None and changed.layer_id == "observations"
    assert victim in changed.ids

    cached = {victim: "v", survivor: survivor_version}
    pages = fetch_all(client, project_id, observations, ids=[victim, survivor])
    assert pages[0].removed == (victim,)
    reconciler = Reconciler(cached, ids=[victim, survivor])
    deletes = [d for p in pages for d in reconciler.add_page(p).deletes]
    deletes += reconciler.finish().deletes
    assert deletes == [victim]


def http_get(url: str) -> tuple[int, bytes]:
    assert url.startswith("http://127.0.0.1:"), url
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        with err:
            return err.code, b""


def test_media_is_cached_by_key_and_a_missing_file_shows_as_not_synced(
    session, tmp_path
):
    client, project_id, layers = session
    pages = fetch_all(client, project_id, layers["observations"])
    attachments = [a for p in pages for f in p.features for a in f.attachments]
    assert attachments
    cache = MediaCache(tmp_path)
    states = []
    for attachment in attachments:
        variant = "thumbnail" if attachment.type == "photo" else "original"
        status, body = http_get(attachment.urls[variant])
        entry = cache.record_fetch(project_id, attachment, variant, status, body)
        states.append(entry.state)
        if entry.state is MediaState.CACHED:
            assert entry.path.suffix in (".jpg", ".m4a", ".mp4")
            assert entry.path.read_bytes() == body
    assert MediaState.CACHED in states
    assert MediaState.NOT_SYNCED in states
