import json
import struct

import pytest

from comapeo_qgis.core.protocol import (
    CONNECTION_CLOSED,
    TIMEOUT,
    UPDATE_APP,
    UPDATE_PLUGIN,
    Authenticated,
    Backoff,
    ClientConnection,
    ClientError,
    ClientState,
    Closed,
    HandshakeRejected,
    Notification,
    ProtocolViolation,
    RequestFailed,
    Response,
    VersionMismatch,
)
from tests.fake_server import frame

SECRET = "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlYw"
HELLO_OK = {
    "id": 1,
    "result": {
        "protocolVersion": 1,
        "server": {"name": "@comapeo/local-api", "version": "1.0.0"},
        "app": {"name": "CoMapeo Desktop", "version": "1.2.3"},
    },
}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def decode_all(data: bytes) -> list:
    messages = []
    while data:
        (size,) = struct.unpack("<I", data[:4])
        messages.append(json.loads(data[4 : 4 + size].decode("utf-8")))
        data = data[4 + size :]
    return messages


def new_client(**kwargs) -> tuple[ClientConnection, Clock]:
    clock = Clock()
    conn = ClientConnection(SECRET, "comapeo-qgis", "0.1.0", clock=clock, **kwargs)
    return conn, clock


def ready_client(**kwargs) -> tuple[ClientConnection, Clock]:
    conn, clock = new_client(**kwargs)
    conn.start()
    conn.data_to_send()
    events = conn.receive_data(frame(HELLO_OK))
    assert isinstance(events[0], Authenticated)
    return conn, clock


def test_first_frame_is_the_hello_from_the_protocol_document():
    conn, _ = new_client()
    assert conn.data_to_send() == b""
    conn.start()
    data = conn.data_to_send()
    assert len(data) <= 4096
    assert decode_all(data) == [
        {
            "id": 1,
            "method": "hello",
            "params": {
                "secret": SECRET,
                "protocolVersions": [1],
                "client": {"name": "comapeo-qgis", "version": "0.1.0"},
            },
        }
    ]


def test_requests_wait_until_the_server_accepts_the_hello():
    conn, _ = new_client()
    conn.start()
    conn.data_to_send()
    request_id = conn.request("projects.list")
    assert conn.data_to_send() == b""
    events = conn.receive_data(frame(HELLO_OK))
    assert events == [
        Authenticated(1, HELLO_OK["result"]["server"], HELLO_OK["result"]["app"])
    ]
    assert conn.state is ClientState.READY
    assert conn.app_info["version"] == "1.2.3"
    assert decode_all(conn.data_to_send()) == [
        {"id": request_id, "method": "projects.list"}
    ]


def test_responses_are_matched_to_requests_out_of_order():
    conn, _ = ready_client()
    a = conn.request("layers.list", {"projectId": "p"})
    b = conn.request("categories.list", {"projectId": "p"})
    sent = decode_all(conn.data_to_send())
    assert [m["params"] for m in sent] == [{"projectId": "p"}, {"projectId": "p"}]
    events = conn.receive_data(
        frame({"id": b, "result": {"categories": []}, "extra": 1})
        + frame({"id": a, "result": {"layers": []}})
    )
    assert events == [
        Response(b, "categories.list", {"categories": []}),
        Response(a, "layers.list", {"layers": []}),
    ]
    assert conn.pending_requests == 0


def test_error_responses_carry_code_message_and_data():
    conn, _ = ready_client()
    rid = conn.request("layers.list", {"projectId": "nope"})
    error = {"code": "PROJECT_NOT_FOUND", "message": "no project", "data": {"x": 1}}
    [event] = conn.receive_data(frame({"id": rid, "error": error}))
    assert isinstance(event, RequestFailed)
    assert event.method == "layers.list"
    assert (event.error.code, event.error.message, event.error.data) == (
        "PROJECT_NOT_FOUND",
        "no project",
        {"x": 1},
    )


def test_notifications_including_unknown_methods_are_passed_on():
    conn, _ = ready_client()
    events = conn.receive_data(
        frame({"method": "layer.changed", "params": {"projectId": "p", "ids": ["a"]}})
        + frame({"method": "something.new", "params": {}})
        + frame({"method": "no.params"})
    )
    assert events == [
        Notification("layer.changed", {"projectId": "p", "ids": ["a"]}),
        Notification("something.new", {}),
        Notification("no.params", {}),
    ]


def test_responses_for_unknown_ids_are_ignored():
    conn, _ = ready_client()
    assert conn.receive_data(frame({"id": 999, "result": {}})) == []
    assert conn.state is ClientState.READY


def test_server_versions_older_than_client_means_update_the_app():
    conn, _ = new_client(protocol_versions=(2, 3))
    conn.start()
    reply = {
        "id": 1,
        "error": {
            "code": "UNSUPPORTED_PROTOCOL_VERSION",
            "message": "no",
            "data": {"supported": [1]},
        },
    }
    events = conn.receive_data(frame(reply))
    assert events[0] == VersionMismatch((2, 3), (1,), UPDATE_APP)
    assert events[-1] == Closed(was_authenticated=False)
    assert conn.wants_close


def test_server_versions_newer_than_client_means_update_the_plugin():
    conn, _ = new_client()
    conn.start()
    reply = {
        "id": 1,
        "error": {
            "code": "UNSUPPORTED_PROTOCOL_VERSION",
            "message": "no",
            "data": {"supported": [2, 3]},
        },
    }
    [mismatch, _closed] = conn.receive_data(frame(reply))
    assert mismatch.update == UPDATE_PLUGIN


def test_version_mismatch_without_supported_list_has_no_update_target():
    conn, _ = new_client()
    conn.start()
    reply = {"id": 1, "error": {"code": "UNSUPPORTED_PROTOCOL_VERSION"}}
    [mismatch, _closed] = conn.receive_data(frame(reply))
    assert mismatch == VersionMismatch((1,), (), None)


def test_server_agreeing_to_a_version_not_offered_is_a_protocol_violation():
    conn, _ = new_client()
    conn.start()
    reply = {"id": 1, "result": {"protocolVersion": 7}}
    events = conn.receive_data(frame(reply))
    assert isinstance(events[0], ProtocolViolation)
    assert conn.state is ClientState.CLOSED


def test_close_before_hello_reply_is_a_rejection_and_fails_queued_requests():
    conn, _ = new_client()
    conn.start()
    rid = conn.request("projects.list")
    events = conn.connection_lost()
    assert isinstance(events[0], HandshakeRejected)
    assert (
        events[1] == RequestFailed(rid, "projects.list", events[1].error)
        and events[1].error.code == CONNECTION_CLOSED
    )
    assert events[-1] == Closed(was_authenticated=False)
    assert conn.connection_lost() == []


def test_close_after_authentication_fails_pending_requests_once():
    conn, _ = ready_client()
    rid = conn.request("projects.list")
    events = conn.connection_lost()
    assert [type(e) for e in events] == [RequestFailed, Closed]
    assert events[0].request_id == rid
    assert events[1].was_authenticated
    assert conn.connection_lost() == []
    with pytest.raises(ClientError):
        conn.request("projects.list")


def test_request_timeout_driven_by_the_injected_clock():
    conn, clock = ready_client(request_timeout=10)
    slow = conn.request("layers.getFeatures", {"projectId": "p", "layerId": "x"})
    clock.now = 4
    quick = conn.request("projects.list", timeout=1)
    assert conn.next_deadline() == 5
    clock.now = 5
    [event] = conn.tick()
    assert event.request_id == quick and event.error.code == TIMEOUT
    clock.now = 9.9
    assert conn.tick() == []
    [event] = conn.tick(now=10)
    assert event.request_id == slow and event.error.code == TIMEOUT
    assert conn.receive_data(frame({"id": slow, "result": {}})) == []
    assert conn.state is ClientState.READY


def test_handshake_timeout():
    conn, clock = new_client(handshake_timeout=3)
    conn.start()
    assert conn.next_deadline() == 3
    clock.now = 2.9
    assert conn.tick() == []
    clock.now = 3
    events = conn.tick()
    assert isinstance(events[0], HandshakeRejected)
    assert conn.wants_close


def test_oversize_frame_from_server_closes():
    conn, _ = ready_client(max_frame_size=200)
    rid = conn.request("projects.list")
    events = conn.receive_data(struct.pack("<I", 201))
    assert isinstance(events[0], ProtocolViolation)
    assert events[1].request_id == rid
    assert conn.wants_close


def test_non_object_message_is_a_protocol_violation():
    conn, _ = ready_client()
    [violation, _closed] = conn.receive_data(frame([1, 2, 3]))
    assert isinstance(violation, ProtocolViolation)


def test_server_stream_split_at_every_byte_boundary():
    stream = (
        frame(HELLO_OK)
        + frame({"id": 2, "result": {"projects": [{"projectId": "p"}]}})
        + frame({"method": "layer.changed", "params": {"ids": ["x"]}})
    )
    expected = None
    for cut in range(len(stream) + 1):
        conn, _ = new_client()
        conn.start()
        conn.request("projects.list")
        events = conn.receive_data(stream[:cut]) + conn.receive_data(stream[cut:])
        expected = expected or events
        assert events == expected, f"cut at {cut}"
    assert [type(e) for e in expected] == [Authenticated, Response, Notification]


def test_local_close_fails_pending_and_asks_transport_to_close():
    conn, _ = ready_client()
    conn.request("projects.list")
    events = conn.close()
    assert [type(e) for e in events] == [RequestFailed, Closed]
    assert conn.wants_close
    assert conn.data_to_send() == b""


def test_backoff_doubles_to_a_maximum_and_resets():
    backoff = Backoff(initial=1, maximum=5)
    assert [backoff.next_delay() for _ in range(5)] == [1, 2, 4, 5, 5]
    backoff.reset()
    assert backoff.next_delay() == 1


def test_request_queued_during_a_slow_handshake_times_out_and_is_never_sent():
    conn, clock = new_client(request_timeout=2, handshake_timeout=10)
    conn.start()
    conn.data_to_send()
    late = conn.request("projects.list")
    assert conn.next_deadline() == 2
    clock.now = 2
    [event] = conn.tick()
    assert event.request_id == late and event.error.code == TIMEOUT
    assert conn.next_deadline() == 10
    conn.receive_data(frame(HELLO_OK))
    assert conn.data_to_send() == b""


def test_server_speaking_before_hello_is_a_protocol_violation():
    conn, _ = new_client()
    events = conn.receive_data(frame(HELLO_OK))
    assert isinstance(events[0], ProtocolViolation)
    assert conn.wants_close


def test_hello_that_cannot_fit_the_handshake_limit_is_a_client_error():
    conn = ClientConnection("s" * 5000, "comapeo-qgis", "0.1.0")
    with pytest.raises(ClientError):
        conn.start()
    assert conn.state is ClientState.IDLE
    assert conn.data_to_send() == b""


@pytest.mark.parametrize(
    "params",
    [{"ids": ["x" * 300]}, {"when": object()}, {"n": float("nan")}],
)
def test_params_that_cannot_be_sent_are_a_client_error(params):
    conn, _ = ready_client(max_frame_size=256)
    with pytest.raises(ClientError):
        conn.request("layers.getFeatures", params)
    assert conn.pending_requests == 0
    assert conn.data_to_send() == b""
    rid = conn.request("projects.list")
    assert decode_all(conn.data_to_send()) == [{"id": rid, "method": "projects.list"}]


@pytest.mark.parametrize(
    "bad",
    [
        struct.pack("<I", 10_000_000),
        struct.pack("<I", 9) + b"{not json",
        struct.pack("<I", 200_000) + ("[" * 100_000 + "]" * 100_000).encode(),
    ],
)
def test_frames_before_a_bad_one_in_the_same_chunk_are_still_delivered(bad):
    conn, _ = ready_client(max_frame_size=1_000_000)
    answered = conn.request("projects.list")
    waiting = conn.request("layers.list", {"projectId": "p"})
    events = conn.receive_data(
        frame({"id": answered, "result": {"projects": []}}) + bad
    )
    assert events[0] == Response(answered, "projects.list", {"projects": []})
    assert isinstance(events[1], ProtocolViolation)
    assert events[2].request_id == waiting
    assert events[2].error.code == CONNECTION_CLOSED
    assert events[-1] == Closed(was_authenticated=True)
    assert conn.wants_close


def test_deeply_nested_json_is_a_protocol_violation_not_a_crash():
    conn, _ = ready_client()
    payload = ("[" * 100_000 + "]" * 100_000).encode()
    events = conn.receive_data(struct.pack("<I", len(payload)) + payload)
    assert isinstance(events[0], ProtocolViolation)
    assert conn.state is ClientState.CLOSED


@pytest.mark.parametrize(
    "first",
    [
        {"method": "layer.changed", "params": {"ids": ["a"]}},
        {"id": 2, "result": {"projects": []}},
        {"id": True, **{k: v for k, v in HELLO_OK.items() if k != "id"}},
        {"id": 1.0, **{k: v for k, v in HELLO_OK.items() if k != "id"}},
        {"id": 1, "result": {**HELLO_OK["result"], "protocolVersion": 1.0}},
        {"id": 1, "result": {**HELLO_OK["result"], "protocolVersion": True}},
    ],
)
def test_anything_but_a_well_formed_hello_reply_during_the_handshake_is_refused(
    first,
):
    conn, _ = new_client()
    conn.start()
    conn.data_to_send()
    queued = conn.request("projects.list")
    events = conn.receive_data(frame(first) + frame(HELLO_OK))
    assert isinstance(events[0], ProtocolViolation)
    assert events[1].request_id == queued
    assert events[-1] == Closed(was_authenticated=False)
    assert conn.protocol_version is None
    assert conn.data_to_send() == b""
