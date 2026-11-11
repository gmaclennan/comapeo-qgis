import pytest

from comapeo_qgis.core.model import (
    FeaturePage,
    FeatureRecord,
    Project,
    parse_categories,
    parse_feature_page,
    parse_layers,
)
from comapeo_qgis.core.reconcile import (
    ReconcileError,
    Reconciler,
    layer_schema_key,
    plan_layers,
    plan_projects,
    reconcile,
)
from tests.fake_server import default_layers, observation


def rec(feature_id, version="v1"):
    return FeatureRecord(feature_id, version, None, {}, None)


def page(*records, removed=(), cursor=None):
    return FeaturePage(tuple(records), tuple(removed), cursor)


def test_full_fetch_upserts_new_and_changed_and_deletes_missing():
    cached = {"a": "v1", "b": "v1", "gone": "v1"}
    plan = reconcile(
        cached, [page(rec("a"), rec("b", "v2"), rec("new"))], refresh_all=False
    )
    assert sorted(f.id for f in plan.upserts) == ["b", "new"]
    assert plan.deletes == ("gone",)
    assert plan.unchanged == 1


def test_full_fetch_deletes_only_after_the_last_page():
    cached = {"a": "v1", "b": "v1", "c": "v1"}
    reconciler = Reconciler(cached, refresh_all=False)
    first = reconciler.add_page(page(rec("a"), cursor="1"))
    assert first.deletes == () and first.upserts == ()
    second = reconciler.add_page(page(rec("c", "v2")))
    assert [f.id for f in second.upserts] == ["c"] and second.deletes == ()
    assert reconciler.finish().deletes == ("b",)
    with pytest.raises(ReconcileError):
        reconciler.add_page(page())


def test_empty_full_fetch_deletes_everything():
    assert reconcile({"a": "v1"}, [page()]).deletes == ("a",)


def test_feature_repeated_across_pages_is_upserted_once():
    plan = reconcile({}, [page(rec("a", "v1"), cursor="1"), page(rec("a", "v2"))])
    assert [(f.id, f.version) for f in plan.upserts] == [("a", "v2")]


def test_feature_repeated_within_a_page_is_still_upserted():
    plan = Reconciler({"a": "v0"}).add_page(page(rec("a", "v1"), rec("a", "v1")))
    assert [f.id for f in plan.upserts] == ["a"]


def test_ids_fetch_only_touches_requested_ids():
    cached = {"a": "v1", "b": "v1", "c": "v1"}
    plan = reconcile(
        cached,
        [page(rec("a", "v2"), rec("x", "v9"), removed=["b", "zz"])],
        ids=["a", "b"],
    )
    assert [f.id for f in plan.upserts] == ["a"]
    assert plan.deletes == ("b",)


def test_removed_is_combined_across_pages():
    cached = {"a": "v1", "b": "v1", "c": "v1"}
    plan = reconcile(
        cached,
        [page(removed=["a"], cursor="1"), page(rec("c", "v2"), removed=["b"])],
        ids=["a", "b", "c"],
    )
    assert sorted(plan.deletes) == ["a", "b"]
    assert [f.id for f in plan.upserts] == ["c"]


def test_requested_id_in_neither_features_nor_removed_is_deleted_at_finish():
    reconciler = Reconciler({"a": "v1", "b": "v1"}, ids=["a", "b"])
    assert reconciler.add_page(page(rec("a"))).deletes == ()
    assert reconciler.finish().deletes == ("b",)


def test_removed_id_not_in_cache_is_not_a_delete():
    assert reconcile({}, [page(removed=["a"])], ids=["a"]).deletes == ()


def test_new_id_from_notification_is_inserted():
    plan = reconcile({"a": "v1"}, [page(rec("n"))], ids=["n"])
    assert [f.id for f in plan.upserts] == ["n"] and plan.deletes == ()


def layers():
    return parse_layers({"layers": default_layers()})


def test_schema_key_changes_with_schema_version_and_language():
    obs = layers()[0]
    key = layer_schema_key(obs, "en")
    assert key == layer_schema_key(obs, "en")
    assert key != layer_schema_key(obs, "es")
    changed = parse_layers(
        {"layers": [{**default_layers()[0], "schemaVersion": "s2"}]}
    )[0]
    assert key != layer_schema_key(changed, "en")


def test_schema_key_without_schema_version_follows_columns():
    raw = {**default_layers()[0]}
    del raw["schemaVersion"]
    a = parse_layers({"layers": [raw]})[0]
    b = parse_layers({"layers": [{**raw, "columns": raw["columns"][:2]}]})[0]
    assert layer_schema_key(a, None) != layer_schema_key(b, None)


def test_plan_layers():
    obs, trk = layers()
    cached = {
        "observations": layer_schema_key(obs, "en"),
        "tracks": layer_schema_key(trk, "es"),
        "old": "whatever",
    }
    future = parse_layers(
        {"layers": [{"layerId": "alerts", "geometryType": "Polygon"}]}
    )[0]
    plan = plan_layers(cached, [obs, trk, future], "en")
    assert [layer.layer_id for layer in plan.unchanged] == ["observations"]
    assert [layer.layer_id for layer in plan.rebuilt] == ["tracks"]
    assert [layer.layer_id for layer in plan.added] == ["alerts"]
    assert plan.removed == ("old",)


def test_finish_refuses_to_delete_before_the_last_page():
    reconciler = Reconciler({"a": "v1", "b": "v1"})
    reconciler.add_page(page(rec("a"), cursor="next"))
    with pytest.raises(ReconcileError):
        reconciler.finish()
    with pytest.raises(ReconcileError):
        Reconciler({"a": "v1"}).finish()


def test_by_default_unchanged_versions_are_still_refreshed():
    cached = {"a": "v1", "b": "v1", "gone": "v1"}
    plan = reconcile(cached, [page(rec("a"), rec("b", "v2"))])
    assert sorted(f.id for f in plan.upserts) == ["a", "b"]
    assert plan.deletes == ("gone",)
    assert plan.unchanged == 0


OBSERVATIONS = parse_layers({"layers": default_layers()})[0]


@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        {"features": "x", "nextCursor": None},
        {"features": []},
        {"features": [{"id": "a"}, {"id": "b"}], "nextCursor": None},
        {"features": [{"version": "v1"}], "nextCursor": None},
    ],
)
def test_a_doubtful_page_never_deletes_the_cache(result):
    bad = parse_feature_page(result, OBSERVATIONS)
    plan = reconcile({"a": "v1", "b": "v1"}, [bad])
    assert plan.deletes == ()
    assert plan.deletes_withheld


def test_a_doubtful_page_mid_fetch_withholds_deletes_but_keeps_upserts():
    good = parse_feature_page(
        {"features": [observation("a", "v2")], "nextCursor": "1"}, OBSERVATIONS
    )
    bad = parse_feature_page({"features": None, "nextCursor": None}, OBSERVATIONS)
    plan = reconcile({"a": "v1", "b": "v1"}, [good, bad])
    assert [f.id for f in plan.upserts] == ["a"]
    assert plan.deletes == () and plan.deletes_withheld


def test_an_ids_fetch_with_a_doubtful_page_deletes_only_what_the_server_removed():
    bad = parse_feature_page(
        {"features": [{"id": "a"}], "removed": ["b"], "nextCursor": None},
        OBSERVATIONS,
    )
    plan = reconcile({"a": "v1", "b": "v1", "c": "v1"}, [bad], ids=["a", "b", "c"])
    assert plan.deletes == ("b",)
    assert plan.deletes_withheld


def test_a_clean_empty_layer_still_empties_the_cache():
    empty = parse_feature_page({"features": [], "nextCursor": None}, OBSERVATIONS)
    plan = reconcile({"a": "v1"}, [empty])
    assert plan.deletes == ("a",) and not plan.deletes_withheld


def categories(name="School", color="#f00"):
    return parse_categories(
        {"categories": [{"categoryId": "c1", "name": name, "color": color}]}
    )


def test_category_changes_while_disconnected_mark_layers_for_rebuild():
    obs, trk = layers()
    stored = {
        "observations": layer_schema_key(obs, "en", categories()),
        "tracks": layer_schema_key(trk, "en", categories()),
    }
    same = plan_layers(stored, [obs, trk], "en", categories())
    assert [layer.layer_id for layer in same.unchanged] == ["observations", "tracks"]
    for changed in (categories(name="Escuela"), categories(color="#00f"), []):
        plan = plan_layers(stored, [obs, trk], "en", changed)
        assert [layer.layer_id for layer in plan.rebuilt] == ["observations", "tracks"]


def test_category_order_does_not_matter():
    obs = layers()[0]
    two = parse_categories(
        {"categories": [{"categoryId": "a", "name": "A"}, {"categoryId": "b"}]}
    )
    assert layer_schema_key(obs, None, two) == layer_schema_key(obs, None, two[::-1])


def test_plan_projects():
    plan = plan_projects(["p1", "left"], [Project("p1", "Rio"), Project("p2", "New")])
    assert [p.project_id for p in plan.kept] == ["p1"]
    assert [p.project_id for p in plan.added] == ["p2"]
    assert plan.gone == ("left",)
