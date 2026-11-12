"""The editing members of the protocol, and what the plugin sends and says."""

from comapeo_qgis.core.editing import (
    UNCHANGED,
    UNSENDABLE,
    EditRequest,
    RowBase,
    RowChange,
    checked_text,
    discard_messages,
    landed,
    lost_text,
    plan_save,
    read_only_message,
    refusal_text,
    summary_text,
)
from comapeo_qgis.core.model import (
    FeatureRecord,
    parse_categories,
    parse_feature_page,
    parse_layers,
)
from comapeo_qgis.core.reconcile import Reconciler, layer_schema_key
from tests.fake_server import default_layers, observation

LIVE = {
    "layerId": "observations",
    "title": "Observations",
    "geometryType": "Point",
    "editable": True,
    "editOperations": ["properties", "category", "delete", "move", "later"],
    "columns": [
        {"name": "id", "type": "string", "label": "ID", "role": "builtin"},
        {"name": "notes", "type": "string", "label": "Notes", "editable": True},
        {
            "name": "building_type",
            "type": "string",
            "label": "Building type",
            "editable": True,
            "categoryIds": ["cat-school"],
        },
        {
            "name": "visited",
            "type": "datetime",
            "label": "Visited",
            "editable": True,
            "categoryIds": ["cat-school"],
        },
        {
            "name": "threats",
            "type": "string_list",
            "label": "Threats",
            "editable": True,
        },
        {"name": "accuracy", "type": "number", "label": "Accuracy"},
    ],
}


def live(**changes):
    return parse_layers({"layers": [{**LIVE, **changes}]})[0]


def base(doc_id="o1", category="cat-school", forks=()):
    return RowBase(doc_id, f"{doc_id}-v1", tuple(forks), category, True)


def test_layers_carry_the_editing_members():
    layer = live(editable=False, readOnlyReason="disabled")
    assert layer.editable is False
    assert layer.read_only_reason == "disabled"
    assert layer.edit_operations == (
        "properties",
        "category",
        "delete",
        "move",
        "later",
    )
    assert layer.column("notes").editable is True
    assert layer.column("notes").category_ids is None
    assert layer.column("building_type").category_ids == ("cat-school",)
    assert layer.column("accuracy").editable is False


def test_a_server_that_predates_editing_has_no_editable_member():
    [old] = parse_layers({"layers": default_layers()[:1]})
    stripped = {k: v for k, v in default_layers()[0].items() if k != "editOperations"}
    [older] = parse_layers({"layers": [stripped]})
    assert old.editable is None and older.editable is None
    assert older.edit_operations == ()
    assert read_only_message(older) == (
        "This version of CoMapeo Desktop does not support editing from QGIS. "
        "Update CoMapeo Desktop to edit observations."
    )


def test_read_only_reasons_in_words():
    assert read_only_message(live(editable=False, readOnlyReason="disabled")) == (
        "Editing from QGIS is switched off in CoMapeo Desktop."
    )
    assert read_only_message(live(editable=False, readOnlyReason="role")) == (
        "Your role in this project does not allow editing."
    )
    tracks = live(editable=False, readOnlyReason="unsupported", title="Tracks")
    assert read_only_message(tracks) == "Tracks cannot be edited from QGIS."
    assert read_only_message(None) == "Editing needs CoMapeo Desktop to be connected."


def test_features_carry_forks_and_editable():
    layer = live()
    item = observation("o1", forks=["z", "a", "a"])
    item["editable"] = True
    page = parse_feature_page({"features": [item], "nextCursor": None}, layer)
    [record] = page.features
    assert record.forks == ("a", "z")
    assert record.editable is True
    old = observation("o2")
    del old["forks"]
    [record] = parse_feature_page(
        {"features": [old], "nextCursor": None}, layer
    ).features
    assert record.forks == () and record.editable is False


def test_categories_say_which_geometries_they_are_for():
    result = {
        "categories": [
            {"categoryId": "a", "name": "A", "geometryTypes": ["Point"]},
            {"categoryId": "b", "name": "B"},
        ]
    }
    a, b = parse_categories(result)
    assert a.offered_for("Point") and not a.offered_for("LineString")
    assert b.offered_for("LineString")


def test_a_late_fork_changes_the_revision_and_is_fetched_again():
    cached = {"o1": FeatureRecord("o1", "v1", None, {}, None).revision}
    late = FeatureRecord("o1", "v1", None, {}, None, forks=("v0",))
    reconciler = Reconciler(cached, ["o1"], refresh_all=False)
    from comapeo_qgis.core.model import FeaturePage

    plan = reconciler.add_page(FeaturePage((late,), (), None))
    assert plan.upserts == (late,)
    again = Reconciler({"o1": late.revision}, ["o1"], refresh_all=False)
    assert again.add_page(FeaturePage((late,), (), None)).upserts == ()


def test_the_cache_format_is_part_of_the_schema_key():
    [layer] = parse_layers({"layers": default_layers()[:1]})
    assert '"f:2"' in layer_schema_key(layer, "en")


def test_one_request_per_feature_with_the_version_and_forks_first_seen():
    changes = [
        RowChange(base("o1", forks=("x",)), values={"notes": "a"}),
        RowChange(base("o2"), category_id="cat-river"),
        RowChange(base("o3"), deleted=True),
        RowChange(base("o4"), values={"building_type": None}),
    ]
    plan = plan_save(live(), "p1", changes, lang="pt")
    assert [(r.method, r.doc_id) for r in plan.requests] == [
        ("features.update", "o1"),
        ("features.update", "o2"),
        ("features.delete", "o3"),
        ("features.update", "o4"),
    ]
    first = plan.requests[0].params
    assert first == {
        "projectId": "p1",
        "layerId": "observations",
        "id": "o1",
        "version": "o1-v1",
        "forks": ["x"],
        "lang": "pt",
        "properties": {"notes": "a"},
    }
    assert plan.requests[1].params["categoryId"] == "cat-river"
    assert "properties" not in plan.requests[2].params
    assert plan.requests[3].params["properties"] == {"building_type": None}
    assert plan.discards == ()


def test_a_geometry_is_never_sent_even_where_move_is_listed():
    plan = plan_save(
        live(), "p1", [RowChange(base(), geometry=True, values={"notes": "n"})]
    )
    [request] = plan.requests
    assert "geometry" not in request.params
    assert discard_messages(plan.discards, {}) == [
        "Change of location discarded for 1 observation: points cannot be moved "
        "from QGIS yet."
    ]
    only_moved = plan_save(live(), "p1", [RowChange(base(), geometry=True)])
    assert only_moved.requests == ()


def test_what_is_certain_to_be_refused_is_dropped_with_a_reason():
    changes = [
        RowChange(base("o1", category="cat-river"), values={"building_type": "x"}),
        RowChange(base("o2"), values={"accuracy": 3.0, "visited": "2026-01-01"}),
        RowChange(base("o3"), values={"threats": ["fire"], "notes": UNSENDABLE}),
        RowChange(base("o4"), category_id=None),
        RowChange(base("o5", category=None), values={"building_type": "x"}),
    ]
    plan = plan_save(live(), "p1", changes, created=2, schema_changed=True)
    assert plan.requests == ()
    labels = {"building_type": "Building type", "accuracy": "Accuracy"}
    assert discard_messages(plan.discards, labels) == [
        "2 new features discarded: observations cannot be created from QGIS.",
        "Column changes are not supported on CoMapeo layers and were discarded.",
        "Change of category discarded for 1 observation: a category cannot be "
        "removed, only changed.",
        "Change to Accuracy discarded: that column cannot be edited.",
        "Change to threats discarded: that column cannot be edited.",
        "Change to visited discarded: that column cannot be edited.",
        "Change to Building type discarded for 2 observations: it is not a field of "
        "their category.",
        "Value for notes could not be sent.",
    ]


def test_a_field_is_sent_with_a_change_to_a_category_that_has_it():
    change = RowChange(
        base("o1", category="cat-river"),
        values={"building_type": "school"},
        category_id="cat-school",
    )
    [request] = plan_save(live(), "p1", [change]).requests
    assert request.params["categoryId"] == "cat-school"
    assert request.params["properties"] == {"building_type": "school"}


def test_kinds_of_edit_the_layer_does_not_list_are_dropped():
    layer = live(editOperations=["properties"])
    changes = [
        RowChange(base("o1"), deleted=True),
        RowChange(base("o2"), category_id="cat-river", values={"notes": "kept"}),
    ]
    plan = plan_save(layer, "p1", changes)
    [request] = plan.requests
    assert request.params["properties"] == {"notes": "kept"}
    assert "categoryId" not in request.params
    kinds = {d.kind for d in plan.discards}
    assert kinds == {"delete", "category"}
    unchanged = RowChange(base("o3"), category_id=UNCHANGED)
    assert plan_save(layer, "p1", [unchanged]).requests == ()


def test_each_refusal_in_words():
    labels = {"water_source": "Water source", "notes": "Notes"}
    cases = [
        ("CONFLICT", {"feature": {}}, "changed in CoMapeo since QGIS last updated it"),
        ("FEATURE_NOT_FOUND", {"deleted": True}, "it was deleted in CoMapeo"),
        (
            "NOT_PERMITTED",
            {"reason": "not-own"},
            "created on another device; your role lets you change only observations "
            "created on this device",
        ),
        (
            "NOT_PERMITTED",
            {"reason": "role"},
            "your role in this project does not allow editing",
        ),
        (
            "NOT_EDITABLE",
            {"target": "column", "column": "notes", "reason": "not-in-category"},
            "Notes is not a field of this observation's category",
        ),
        (
            "NOT_EDITABLE",
            {"target": "column", "column": "notes"},
            "Notes cannot be edited",
        ),
        ("NOT_EDITABLE", {"target": "geometry"}, "points cannot be moved"),
        (
            "INVALID_VALUE",
            {"target": "column", "column": "water_source", "reason": "not-an-option"},
            "Water source: value not accepted, not one of the field's options",
        ),
        (
            "INVALID_VALUE",
            {
                "target": "column",
                "column": "water_source",
                "reason": "would-change-category",
            },
            "Water source: this value would change the observation's category; "
            "change the category instead",
        ),
        (
            "INVALID_VALUE",
            {"target": "category", "reason": "category-would-not-show"},
            "the category could not be changed because the observation's other tags "
            "still match another category",
        ),
        (
            "INVALID_VALUE",
            {"target": "column", "column": "x", "reason": "a-new-reason"},
            "x: value not accepted, value not accepted",
        ),
        (
            "EDITING_DISABLED",
            None,
            "editing from QGIS is switched off in CoMapeo Desktop",
        ),
        ("SOMETHING_NEW", None, "refused (SOMETHING_NEW)"),
    ]
    for code, data, text in cases:
        assert refusal_text(code, data, labels) == text, code


def test_summary():
    assert summary_text(1, 0) == "1 change saved to CoMapeo."
    assert summary_text(3, 2) == (
        "3 saved, 2 not saved. The layer shows what CoMapeo holds."
    )
    assert summary_text(0, 0, overlapped=1) == (
        "0 saved, 1 deletion met another change made at the same moment. "
        "The layer shows what CoMapeo holds."
    )
    assert summary_text(2, 1, 1, overlapped=2) == (
        "2 saved, 1 not saved, 1 not confirmed, 2 deletions met another change "
        "made at the same moment. The layer shows what CoMapeo holds."
    )


def test_unconfirmed_changes_in_words():
    assert lost_text(1, closed=True) == (
        "The connection to CoMapeo Desktop closed before it confirmed 1 change. "
        "The plugin checks it once it is connected again."
    )
    assert lost_text(2, closed=False) == (
        "CoMapeo Desktop did not confirm 2 changes. The plugin is checking "
        "whether they were saved."
    )
    assert checked_text(1, 0) == (
        "Of the 1 change CoMapeo Desktop had not confirmed, 1 was saved and 0 were not."
    )
    assert checked_text(1, 2) == (
        "Of the 3 changes CoMapeo Desktop had not confirmed, 1 was saved and 2 "
        "were not."
    )
    assert checked_text(2, 1) == (
        "Of the 3 changes CoMapeo Desktop had not confirmed, 2 were saved and 1 "
        "was not."
    )


def test_whether_an_unconfirmed_edit_landed():
    sent = EditRequest(
        "features.update",
        {"id": "o1", "properties": {"notes": " new "}, "categoryId": "c2"},
        base(),
    )
    applied = {
        "id": "o1",
        "version": "o1-v2",
        "categoryId": "c2",
        "properties": {"notes": "new"},
    }
    assert landed(sent, applied)
    assert not landed(sent, {**applied, "version": "o1-v1"})
    assert not landed(sent, {**applied, "properties": {"notes": "other"}})
    assert not landed(sent, None)
    delete = EditRequest("features.delete", {"id": "o1"}, base())
    assert landed(delete, None)
    assert not landed(delete, applied)
