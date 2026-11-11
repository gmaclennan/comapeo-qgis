import base64
from datetime import datetime, timedelta, timezone

import pytest

from comapeo_qgis.core.model import (
    KIND_BOOLEAN,
    KIND_DATETIME,
    KIND_INTEGER,
    KIND_MAP,
    KIND_REAL,
    KIND_STRING,
    KIND_STRING_LIST,
    parse_categories,
    parse_datetime,
    parse_feature_page,
    parse_layer_changed,
    parse_layers,
    parse_projects,
    parse_schema_changed,
)
from tests.fake_server import default_layers, observation, track

LAYERS_RESULT = {
    "layers": [
        {
            "layerId": "observations",
            "title": "Observations",
            "docType": "observation",
            "geometryType": "Point",
            "schemaVersion": "abc",
            "futureProperty": True,
            "columns": [
                {"name": "id", "type": "string", "label": "ID", "role": "builtin"},
                {"name": "accuracy", "type": "number", "label": "Accuracy"},
                {"name": "photo_count", "type": "integer", "label": "Photos"},
                {"name": "verified", "type": "boolean", "label": "Verified"},
                {"name": "created_at", "type": "datetime", "label": "Created"},
                {
                    "name": "threats",
                    "type": "string_list",
                    "label": "Threats",
                    "role": "field",
                    "options": [
                        {"value": "fire", "label": "Fire"},
                        {"value": 3},
                        "garbage",
                    ],
                },
                {"name": "tags", "type": "json", "label": "All tags", "role": "tags"},
                {"name": "geo_shape", "type": "polygon3d", "label": "Shape"},
                {"name": "comapeo_category", "type": "string", "role": "builtin"},
                {"name": "category", "type": "string", "role": "field"},
                {"name": "category", "type": "number", "role": "field"},
            ],
        },
        {"title": "no id"},
        {"layerId": "future", "geometryType": "Polygon", "columns": []},
    ]
}


@pytest.fixture
def observations_layer():
    return parse_layers(LAYERS_RESULT)[0]


def test_layer_definitions_map_column_types_to_field_kinds(observations_layer):
    layer = observations_layer
    assert (layer.layer_id, layer.title, layer.doc_type) == (
        "observations",
        "Observations",
        "observation",
    )
    assert layer.geometry_type == "Point" and layer.schema_version == "abc"
    kinds = {c.name: c.kind for c in layer.columns}
    assert kinds == {
        "id": KIND_STRING,
        "accuracy": KIND_REAL,
        "photo_count": KIND_INTEGER,
        "verified": KIND_BOOLEAN,
        "created_at": KIND_DATETIME,
        "threats": KIND_STRING_LIST,
        "tags": KIND_MAP,
        "geo_shape": KIND_STRING,
        "comapeo_category": KIND_STRING,
        "category": KIND_STRING,
    }


def test_server_resolved_names_are_kept_and_duplicates_dropped(observations_layer):
    names = [c.name for c in observations_layer.columns]
    assert names.count("category") == 1
    assert "comapeo_category" in names
    assert observations_layer.column("comapeo_category").label == "comapeo_category"


def test_options_and_roles(observations_layer):
    threats = observations_layer.column("threats")
    assert threats.role == "field"
    assert [(o.value, o.label) for o in threats.options] == [("fire", "Fire"), (3, "3")]
    assert observations_layer.column("geo_shape").type == "polygon3d"


def test_layers_without_id_are_skipped_and_unknown_geometry_kept():
    layers = parse_layers(LAYERS_RESULT)
    assert [layer.layer_id for layer in layers] == ["observations", "future"]
    assert layers[0].geometry_supported
    assert not layers[1].geometry_supported
    assert layers[1].title == "future"


def feature(**overrides):
    base = observation("o1")
    base.update(overrides)
    return base


def page_of(layer, *features, **extra):
    result = {"features": list(features), "nextCursor": None, **extra}
    return parse_feature_page(result, layer)


def test_point_feature_to_record(observations_layer):
    props = {
        "id": "o1",
        "accuracy": 4,
        "photo_count": 2,
        "verified": True,
        "created_at": "2026-03-04T05:06:07.123Z",
        "threats": ["fire", "logging"],
        "tags": {"threats": ["fire", "logging"], "n": 1},
    }
    page = page_of(
        observations_layer,
        feature(
            properties=props,
            attachments=[
                {
                    "type": "photo",
                    "name": "a.jpg",
                    "key": "k1",
                    "urls": {"thumbnail": "http://127.0.0.1:1/t", "original": 5},
                },
                {"type": "photo", "name": "no key"},
            ],
        ),
    )
    [record] = page.features
    assert record.id == "o1" and record.version == "v1"
    assert record.category_id == "cat-school"
    assert record.geometry.wkt() == "Point (-58.1 -3.2)"
    attrs = record.attributes
    assert attrs["accuracy"] == 4.0 and isinstance(attrs["accuracy"], float)
    assert attrs["photo_count"] == 2
    assert attrs["verified"] is True
    assert attrs["created_at"] == datetime(
        2026, 3, 4, 5, 6, 7, 123000, tzinfo=timezone.utc
    )
    assert attrs["threats"] == ["fire", "logging"]
    assert attrs["tags"] == {"threats": ["fire", "logging"], "n": 1}
    assert list(attrs) == [c.name for c in observations_layer.columns]
    assert attrs["geo_shape"] is None
    [attachment] = record.attachments
    assert attachment.key == "k1"
    assert dict(attachment.urls) == {"thumbnail": "http://127.0.0.1:1/t"}


def test_null_geometry_keeps_the_row(observations_layer):
    [record] = page_of(observations_layer, observation("o1", lon=None)).features
    assert record.geometry is None
    assert record.attributes["id"] == "o1"


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Point", "coordinates": ["a", 1]},
        {"type": "Point", "coordinates": [1]},
        {"type": "Point", "coordinates": [float("nan"), 1]},
        {"type": "Point"},
        {"type": "LineString", "coordinates": [[1, 2]]},
        {"type": "Point", "coordinates": [10**400, 1]},
        {"type": "LineString", "coordinates": [[0, 0], [1, -(10**400)]]},
        "nonsense",
    ],
)
def test_malformed_geometry_becomes_null(observations_layer, geometry):
    [record] = page_of(observations_layer, feature(geometry=geometry)).features
    assert record.geometry is None


def test_geometry_of_the_wrong_type_for_the_layer_becomes_null(observations_layer):
    line = {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}
    [record] = page_of(observations_layer, feature(geometry=line)).features
    assert record.geometry is None


def test_point_with_altitude_keeps_z(observations_layer):
    point = {"type": "Point", "coordinates": [1.5, 2, 30.25]}
    [record] = page_of(observations_layer, feature(geometry=point)).features
    assert record.geometry.wkt() == "PointZ (1.5 2 30.25)"


def tracks_layer():
    return parse_layers({"layers": default_layers()})[1]


def test_track_times_become_m_values_in_epoch_seconds():
    [record] = page_of(tracks_layer(), track("t1")).features
    start = datetime(2026, 1, 1, 10, tzinfo=timezone.utc).timestamp()
    assert record.geometry.m_values == (start, start + 300)
    assert record.geometry.wkt() == (
        f"LineStringM (-58 -3 {int(start)}, -58.1 -3.1 {int(start) + 300})"
    )
    assert record.attachments == ()


def test_times_inside_the_geometry_are_not_where_the_protocol_puts_them():
    t = track("t1")
    t["geometry"]["times"] = t.pop("times")
    [record] = page_of(tracks_layer(), t).features
    assert record.geometry.m_values is None


@pytest.mark.parametrize(
    "times",
    [
        None,
        ["2026-01-01T10:00:00Z"],
        ["2026-01-01T10:00:00Z", "not a time"],
        ["2026-01-01T10:00:00Z", "2026-01-01 10:05:00+24:00"],
        ["2026-01-01T10:00:00Z", "2026-01-01T10:05:00-99:59"],
        "2026-01-01T10:00:00Z",
    ],
)
def test_track_without_usable_times_has_no_m(times):
    t = track("t1")
    if times is None:
        del t["times"]
    else:
        t["times"] = times
    [record] = page_of(tracks_layer(), t).features
    assert record.geometry.m_values is None
    assert record.geometry.wkt().startswith("LineString (")


def test_mixed_dimension_track_drops_z():
    t = track("t1")
    t["geometry"]["coordinates"] = [[0, 0, 5], [1, 1]]
    del t["times"]
    [record] = page_of(tracks_layer(), t).features
    assert record.geometry.wkt() == "LineString (0 0, 1 1)"


def test_features_without_id_or_version_are_skipped(observations_layer):
    page = page_of(
        observations_layer,
        observation("ok"),
        {"version": "v"},
        {"id": "x"},
        "junk",
    )
    assert [f.id for f in page.features] == ["ok"]
    assert page.skipped == 3


def test_one_hostile_value_does_not_cost_the_rest_of_the_page():
    layer = kind_layer("datetime")
    items = [
        {"id": "a", "version": "1", "geometry": None, "properties": {"v": v}}
        for v in ("2026-01-01T00:00:00Z", "2026-01-01 00:00:00+24:00")
    ]
    page = page_of(layer, *items)
    assert [f.attributes["v"] for f in page.features] == [
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        None,
    ]
    assert page.valid and page.skipped == 0


@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        {"features": "x", "nextCursor": None},
        {"features": None, "nextCursor": None},
        {"features": []},
    ],
)
def test_malformed_results_are_flagged_not_mistaken_for_an_empty_layer(
    observations_layer, result
):
    page = parse_feature_page(result, observations_layer)
    assert not page.valid
    assert page.features == ()


def test_a_well_formed_empty_result_is_valid(observations_layer):
    page = parse_feature_page({"features": [], "nextCursor": None}, observations_layer)
    assert page.valid and page.is_last


def test_missing_properties_are_null(observations_layer):
    [record] = page_of(observations_layer, feature(properties=None)).features
    assert set(record.attributes.values()) == {None}


def test_page_cursor_and_removed(observations_layer):
    page = page_of(observations_layer, nextCursor="abc", removed=["r1", 3, ""])
    assert page.next_cursor == "abc" and not page.is_last
    assert page.removed == ("r1",)
    assert page_of(observations_layer).is_last


def kind_layer(kind):
    result = {
        "layers": [
            {
                "layerId": "l",
                "geometryType": "Point",
                "columns": [{"name": "v", "type": kind}],
            }
        ]
    }
    return parse_layers(result)[0]


def value_as(column_type, value):
    layer = kind_layer(column_type)
    item = {"id": "a", "version": "1", "geometry": None, "properties": {"v": value}}
    return page_of(layer, item).features[0].attributes["v"]


@pytest.mark.parametrize(
    ("column_type", "value", "expected"),
    [
        ("string", ["a", "b"], '["a", "b"]'),
        ("string", {"b": 1, "a": 2}, '{"a": 2, "b": 1}'),
        ("string", True, "true"),
        ("string", 3, "3"),
        ("string", 2.5, "2.5"),
        ("mystery", 7, "7"),
        ("number", "12.5", 12.5),
        ("number", "abc", None),
        ("number", True, None),
        ("integer", 4.0, 4),
        ("integer", 4.5, None),
        ("integer", False, None),
        ("integer", "17", 17),
        ("boolean", "TRUE", True),
        ("boolean", 1, None),
        ("string_list", "single", ["single"]),
        ("string_list", ["a", None, 2, True], ["a", "2", "true"]),
        ("datetime", "garbage", None),
        ("datetime", 1234, None),
        ("datetime", "2026-01-01 00:00:00+24:00", None),
        ("datetime", "2026-01-01T00:00:00-99:59", None),
        ("number", 10**400, None),
        ("number", 2**53, float(2**53)),
        ("integer", 10**400, None),
        ("integer", 1e300, None),
        ("integer", 2**63, None),
        ("integer", 2**63 - 1, 2**63 - 1),
        ("integer", -(2**63), -(2**63)),
        ("integer", "9" * 30, None),
        ("json", [1, 2], [1, 2]),
        ("json", None, None),
    ],
)
def test_value_conversion(column_type, value, expected):
    assert value_as(column_type, value) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-01-02T03:04:05Z", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
        (
            "2026-01-02T03:04:05.1234567+02:00",
            datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone(timedelta(hours=2))),
        ),
        ("2026-01-02", datetime(2026, 1, 2, tzinfo=timezone.utc)),
        ("2026-01-02T03:04", datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)),
        (
            "2026-01-02T03:04:05-23:59",
            datetime(
                2026, 1, 2, 3, 4, 5, tzinfo=timezone(-timedelta(hours=23, minutes=59))
            ),
        ),
        ("2026-13-02T03:04:05Z", None),
        ("yesterday", None),
        ("2026-01-01 00:00:00+24:00", None),
        ("2026-01-01 00:00:00+99:00", None),
        ("2026-01-01T00:00:00-99:59", None),
        ("2026-01-01T00:00:00-2500", None),
        ("2026-01-01T00:00:00+4500", None),
        ("2026-01-01T00:00:00+01:60", None),
    ],
)
def test_datetime_parsing(text, expected):
    assert parse_datetime(text) == expected


def test_categories_with_svg_and_broken_icons():
    svg = b"<svg/>"
    result = {
        "categories": [
            {
                "categoryId": "c1",
                "name": "School",
                "color": "#f00",
                "icon": {
                    "mimeType": "image/svg+xml",
                    "data": base64.b64encode(svg).decode(),
                },
            },
            {"categoryId": "c2", "name": "River", "icon": {"mimeType": "image/png"}},
            {"categoryId": "c3", "icon": {"mimeType": "image/png", "data": "!!!"}},
            {
                "categoryId": "c4",
                "icon": {
                    "mimeType": "text/html",
                    "data": base64.b64encode(b"<script>").decode(),
                },
            },
            {
                "categoryId": "c5",
                "icon": {
                    "mimeType": "image/png",
                    "data": base64.b64encode(b"P").decode(),
                },
            },
            {"name": "no id"},
        ]
    }
    categories = parse_categories(result)
    assert [c.category_id for c in categories] == ["c1", "c2", "c3", "c4", "c5"]
    assert categories[0].icon.data == svg
    assert categories[0].icon.mime_type == "image/svg+xml"
    assert categories[0].color == "#f00"
    assert categories[1].icon is None and categories[1].color is None
    assert categories[2].icon is None and categories[2].name == ""
    assert categories[3].icon is None
    assert categories[4].icon.mime_type == "image/png"


def test_projects():
    result = {
        "projects": [
            {"projectId": "p1", "name": "Rio", "color": "#123", "extra": 1},
            {"projectId": "p2"},
            {"name": "no id"},
        ]
    }
    projects = parse_projects(result)
    assert [(p.project_id, p.name, p.color) for p in projects] == [
        ("p1", "Rio", "#123"),
        ("p2", None, None),
    ]
    assert parse_projects(None) == []


def test_layer_changed_params():
    event = parse_layer_changed(
        {"projectId": "p", "layerId": "observations", "ids": ["a", 3, "", "b", "a"]}
    )
    assert (event.project_id, event.layer_id, event.ids) == (
        "p",
        "observations",
        ("a", "b"),
    )
    assert parse_layer_changed({"projectId": "p", "layerId": "l", "ids": []}).ids == ()


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"projectId": "p", "layerId": "l"},
        {"projectId": "p", "layerId": "l", "ids": "a"},
        {"projectId": 1, "layerId": "l", "ids": []},
        {"projectId": "p", "layerId": "", "ids": []},
    ],
)
def test_layer_changed_params_that_cannot_be_acted_on(params):
    assert parse_layer_changed(params) is None


def test_schema_changed_params():
    event = parse_schema_changed({"projectId": "p", "layerId": "tracks", "x": 1})
    assert (event.project_id, event.layer_id) == ("p", "tracks")
    assert parse_schema_changed({"projectId": "p"}) is None
    assert parse_schema_changed([]) is None
