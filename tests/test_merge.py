"""Three-way offline merge: CSV round-trip, conflict rules, preview/apply."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import (
    BatchOp,
    BomStore,
    CyclicStructureError,
    Edge,
    StalePreviewError,
    UnresolvedConflictError,
    ValidationError,
    edges_csv,
    parse_edges_csv,
    parse_resolutions,
    resolve_merge,
    three_way_merge,
)


def _edges(store: BomStore) -> dict[tuple[str, str], int]:
    return {
        (e["parent"], e["child"]): e["qty"] for e in store.snapshot()["edges"]
    }


def _commit(store: BomStore, *pairs, deletes=()):
    ops = [BatchOp("upsert", upserts=(Edge(p, c, q),)) for p, c, q in pairs]
    ops += [BatchOp("delete", parent=p, child=c) for p, c in deletes]
    return store.commit_batch(ops)


def _csv(rows: dict[tuple[str, str], int]) -> str:
    return edges_csv([Edge(p, c, q) for (p, c), q in rows.items()])


# -- CSV round-trip -------------------------------------------------------


def test_csv_round_trip_and_missing_rows_mean_delete():
    rows = {("FG-1", "SA-1"): 2, ("SA-1", "L-1"): 5}
    parsed = parse_edges_csv(_csv(rows))
    assert {(e.parent, e.child): e.qty for e in parsed} == rows
    # A file without the second row simply does not contain it.
    trimmed = parse_edges_csv("parent,child,qty\nFG-1,SA-1,2\n")
    assert [(e.parent, e.child) for e in trimmed] == [("FG-1", "SA-1")]


@pytest.mark.parametrize(
    "body",
    [
        "",
        "a,b\n1,2\n",
        "parent,child\nA,B\n",
        "parent,child,qty\nA,B,0\n",
        "parent,child,qty\nA B,C,1\n",
        "parent,child,qty\nA,B,1\nA,B,2\n",
    ],
)
def test_bad_csv_rejected(body):
    with pytest.raises(ValidationError):
        parse_edges_csv(body)


def test_csv_accepts_bom_padded_header_and_crlf():
    parsed = parse_edges_csv("﻿ parent , Child , qty \r\n a , b ,3\r\n")
    assert [(e.parent, e.child, e.qty) for e in parsed] == [("A", "B", 3)]


# -- three-way comparison rules ------------------------------------------


def test_only_one_side_changed_takes_that_side():
    base = [Edge("A", "B", 1), Edge("A", "C", 1)]
    upload = [Edge("A", "B", 4), Edge("A", "C", 1)]  # qty changed offline
    current = [Edge("A", "B", 1), Edge("A", "C", 9)]  # C changed on the web
    final, changes, conflicts = resolve_merge(base, upload, current, {})
    assert conflicts == []
    assert {(e.parent, e.child): e.qty for e in final} == {("A", "B"): 4, ("A", "C"): 9}
    decisions = {(c.parent, c.child): c.decision for c in changes}
    assert decisions == {("A", "B"): "upload", ("A", "C"): "current"}


def test_both_sides_same_value_merges_without_conflict():
    base = [Edge("A", "B", 1)]
    upload = [Edge("A", "B", 7)]
    current = [Edge("A", "B", 7)]
    final, changes, conflicts = resolve_merge(base, upload, current, {})
    assert conflicts == []
    assert [(c.decision, c.result) for c in changes] == [("merged", 7)]
    assert {(e.parent, e.child): e.qty for e in final} == {("A", "B"): 7}


def test_both_sides_different_value_is_a_conflict():
    base = [Edge("A", "B", 1)]
    upload = [Edge("A", "B", 2)]
    current = [Edge("A", "B", 3)]
    changes, conflicts = three_way_merge(base, upload, current)
    assert changes == []
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert (conflict.base, conflict.upload, conflict.current) == (1, 2, 3)
    assert conflict.decision == "conflict"


def test_conflicts_are_unresolved_and_block_apply():
    base = [Edge("A", "B", 1)]
    upload = [Edge("A", "B", 2)]
    current = [Edge("A", "B", 3)]
    with pytest.raises(UnresolvedConflictError) as info:
        resolve_merge(base, upload, current, {})
    assert info.value.conflicts[0]["upload"] == 2


def test_conflict_can_pick_current_or_upload():
    base = [Edge("A", "B", 1)]
    upload = [Edge("A", "B", 2)]
    current = [Edge("A", "B", 3)]
    chosen_current, _, _ = resolve_merge(base, upload, current, {("A", "B"): "current"})
    chosen_upload, _, _ = resolve_merge(base, upload, current, {("A", "B"): "upload"})
    assert [e.qty for e in chosen_current] == [3]
    assert [e.qty for e in chosen_upload] == [2]


def test_resolutions_are_validated_and_stale_choices_ignored():
    # Choices are only accepted for rows that are conflicts.
    with pytest.raises(ValidationError):
        parse_resolutions([{"parent": "A", "child": "B", "choice": "neither"}])
    base = [Edge("A", "B", 1)]
    upload = [Edge("A", "B", 2)]
    # The library now agrees with the upload: no conflict remains, so an old
    # choice for this row is simply ignored instead of blocking the preview.
    final, changes, conflicts = resolve_merge(
        base, upload, [Edge("A", "B", 2)], {("A", "B"): "upload"}
    )
    assert conflicts == []
    assert {(e.parent, e.child): e.qty for e in final} == {("A", "B"): 2}


def test_preview_keeps_current_value_for_unresolved_rows(store):
    _commit(store, ("A", "B", 1))
    revision = store.create_offline_revision()
    _commit(store, ("A", "B", 3))
    upload = parse_edges_csv("parent,child,qty\nA,B,2\n")
    preview = store.preview_merge(revision["revision_id"], upload)
    # The displayed final structure is provisional (current value kept) but
    # it is not applicable until the conflict is resolved.
    assert preview["final_edges"] == [{"parent": "A", "child": "B", "qty": 3}]
    assert preview["applicable"] is False


def test_delete_participates_in_comparison():
    base = [Edge("A", "B", 1), Edge("A", "C", 1), Edge("A", "D", 1)]
    # Offline deleted B; web deleted C; D kept offline but changed on the web.
    upload = [Edge("A", "C", 1), Edge("A", "D", 1)]
    current = [Edge("A", "B", 1), Edge("A", "D", 5)]
    final, changes, conflicts = resolve_merge(base, upload, current, {})
    assert conflicts == []
    assert {(e.parent, e.child): e.qty for e in final} == {("A", "D"): 5}
    decisions = {(c.parent, c.child): (c.decision, c.result, c.kind) for c in changes}
    # B was deleted offline and untouched on the web -> offline wins.
    assert decisions[("A", "B")] == ("upload", None, "deleted")
    # C was deleted on the web and untouched offline -> web wins.
    assert decisions[("A", "C")] == ("current", None, "deleted")
    # D unchanged in the file but changed on the web -> web value, not listed
    # as a delete.
    assert decisions[("A", "D")] == ("current", 5, "changed")


def test_delete_versus_value_change_conflicts():
    base = [Edge("A", "B", 1)]
    upload: list[Edge] = []  # offline row removed
    current = [Edge("A", "B", 4)]  # web set a new qty
    changes, conflicts = three_way_merge(base, upload, current)
    assert changes == []
    assert len(conflicts) == 1
    assert (conflicts[0].base, conflicts[0].upload, conflicts[0].current) == (1, None, 4)
    # Choosing "upload" deletes the row, choosing "current" keeps 4.
    final_upload, _, _ = resolve_merge(base, upload, current, {("A", "B"): "upload"})
    final_current, _, _ = resolve_merge(base, upload, current, {("A", "B"): "current"})
    assert final_upload == []
    assert [(e.parent, e.child, e.qty) for e in final_current] == [("A", "B", 4)]


# -- store flow -----------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    return BomStore(str(tmp_path / "merge.db"))


def test_revision_is_immutable_and_reports_its_version(store):
    first = _commit(store, ("A", "B", 2))
    revision = store.create_offline_revision()
    _commit(store, ("A", "C", 3))
    again = store.get_offline_revision(revision["revision_id"])
    assert again["csv"] == revision["csv"] == "parent,child,qty\nA,B,2\n"
    assert again["version_seq"] == revision["version_seq"] == first.version_id
    # A new download sees the new structure under a different id.
    newer = store.create_offline_revision()
    assert newer["revision_id"] != revision["revision_id"]
    assert "A,C,3" in newer["csv"]


def test_preview_apply_merges_offline_and_web_changes_with_usage(store):
    _commit(store, ("FG", "SA", 2), ("SA", "L1", 3), ("SA", "L2", 1))
    base_version = store.snapshot()["version"]["version_seq"]
    revision = store.create_offline_revision()

    # Web edit after download: change FG->SA quantity.
    _commit(store, ("FG", "SA", 4))

    # Offline edit: change SA->L1 and delete SA->L2, add SA->L3.
    upload = parse_edges_csv(
        "parent,child,qty\nFG,SA,2\nSA,L1,10\nSA,L3,2\n"  # SA->L2 removed
    )
    preview = store.preview_merge(revision["revision_id"], upload)
    assert preview["base_version"] == base_version
    assert preview["current_version"] == base_version + 1
    assert preview["conflicts"] == []
    assert preview["applicable"] is True

    applied = store.apply_merge(preview["preview_id"])
    assert applied["applied_version"] == applied["version_id"]
    edges = _edges(store)
    assert edges == {("FG", "SA"): 4, ("SA", "L1"): 10, ("SA", "L3"): 2}
    usage = {
        (r["top_part"], r["leaf_part"]): r["qty"]
        for r in store.snapshot()["leaf_usage"]
    }
    # Structure and materialised usage took effect together.
    assert usage[("FG", "L1")] == 40
    assert usage[("FG", "L3")] == 8
    assert ("FG", "L2") not in usage


def test_unresolved_conflicts_block_apply_and_store_nothing(store):
    _commit(store, ("A", "B", 1))
    revision = store.create_offline_revision()
    _commit(store, ("A", "B", 3))  # web
    upload = parse_edges_csv("parent,child,qty\nA,B,2\n")  # offline

    preview = store.preview_merge(revision["revision_id"], upload)
    assert preview["resolved"] is False and preview["applicable"] is False
    assert preview["conflicts"][0]["base"] == 1
    with pytest.raises(UnresolvedConflictError):
        store.apply_merge(preview["preview_id"])
    assert _edges(store) == {("A", "B"): 3}

    resolved = store.preview_merge(
        revision["revision_id"], upload, {("A", "B"): "upload"}
    )
    assert resolved["applicable"] is True and resolved["resolved"] is True
    # The comparison still shows the conflict, now with the chosen value in
    # the final structure.
    assert resolved["conflicts"][0]["upload"] == 2
    assert resolved["final_edges"] == [{"parent": "A", "child": "B", "qty": 2}]
    store.apply_merge(resolved["preview_id"])
    assert _edges(store) == {("A", "B"): 2}


def test_stale_preview_is_rejected_after_another_commit(store):
    _commit(store, ("A", "B", 1))
    revision = store.create_offline_revision()
    upload = parse_edges_csv("parent,child,qty\nA,B,1\nA,C,5\n")
    preview = store.preview_merge(revision["revision_id"], upload)
    assert preview["applicable"] is True

    # Someone commits through the ordinary editor after the preview.
    _commit(store, ("A", "B", 9))
    with pytest.raises(StalePreviewError) as info:
        store.apply_merge(preview["preview_id"])
    assert info.value.current_version == info.value.preview_version + 1
    # The newer edit survives; the old confirmation did nothing.
    assert _edges(store) == {("A", "B"): 9}
    # A fresh preview sees the new value and can be applied.
    fresh = store.preview_merge(revision["revision_id"], upload)
    assert fresh["current_version"] > preview["current_version"]
    store.apply_merge(fresh["preview_id"])
    assert _edges(store) == {("A", "B"): 9, ("A", "C"): 5}


def test_preview_cannot_be_applied_twice(store):
    _commit(store, ("A", "B", 1))
    revision = store.create_offline_revision()
    upload = parse_edges_csv("parent,child,qty\nA,B,2\n")
    preview = store.preview_merge(revision["revision_id"], upload)
    store.apply_merge(preview["preview_id"])
    with pytest.raises(Exception) as info:
        store.apply_merge(preview["preview_id"])
    assert "已应用" in str(info.value)


def test_cycle_after_merge_is_reported_and_nothing_changes(store):
    _commit(store, ("A", "B", 1), ("B", "C", 1))
    revision = store.create_offline_revision()
    upload = parse_edges_csv("parent,child,qty\nA,B,1\nB,C,1\nC,A,1\n")
    before = store.snapshot()
    with pytest.raises(CyclicStructureError) as info:
        store.preview_merge(revision["revision_id"], upload)
    assert info.value.paths == [["A", "B", "C", "A"]]
    assert store.snapshot() == before


def test_cycle_created_by_web_edit_since_download_is_reported(store):
    _commit(store, ("A", "B", 1), ("B", "C", 1))
    revision = store.create_offline_revision()
    # Web adds C->A (acyclic? no - it closes the loop, so the batch is rejected)
    # Instead the web adds B->A?  That would already be a cycle.  Use a new part.
    _commit(store, ("C", "D", 1))
    # Offline adds D->A, which is only cyclic with the web's commit included.
    upload = parse_edges_csv("parent,child,qty\nA,B,1\nB,C,1\nD,A,1\n")
    with pytest.raises(CyclicStructureError) as info:
        store.preview_merge(revision["revision_id"], upload)
    assert info.value.paths[0][0] == info.value.paths[0][-1]
    assert _edges(store) == {
        ("A", "B"): 1, ("B", "C"): 1, ("C", "D"): 1
    }


def test_merge_does_not_hold_write_lock_while_other_editor_commits(tmp_path):
    """Preview/apply on app A must not block an ordinary batch on app B."""
    path = str(tmp_path / "parallel.db")
    store_a = BomStore(path)
    store_b = BomStore(path)
    store_a.commit_batch(
        [BatchOp("upsert", upserts=(Edge("FG", "SA", 2),)),
         BatchOp("upsert", upserts=(Edge("SA", "L1", 3),))]
    )
    revision = store_a.create_offline_revision()
    upload = parse_edges_csv("parent,child,qty\nFG,SA,2\nSA,L1,3\nSA,L2,1\n")

    done = {}

    def other_writer():
        done["result"] = store_b.commit_batch(
            [BatchOp("upsert", upserts=(Edge("FG", "L0", 1),))]
        )

    thread = threading.Thread(target=other_writer)
    thread.start()
    preview = store_a.preview_merge(revision["revision_id"], upload)
    thread.join(timeout=10)
    assert done["result"].version_id > 0
    # The concurrent edit happened after preview -> applying must be refused,
    # but the preview itself never blocked the other writer.
    with pytest.raises(StalePreviewError):
        store_a.apply_merge(preview["preview_id"])
    assert ("FG", "L0") in _edges(store_a)


def test_apply_failure_keeps_old_version_and_usage(store, monkeypatch):
    _commit(store, ("A", "B", 2), ("B", "C", 3))
    revision = store.create_offline_revision()
    preview = store.preview_merge(
        revision["revision_id"], parse_edges_csv("parent,child,qty\nA,B,2\nB,C,3\nB,D,1\n")
    )
    before = store.snapshot()

    def boom(self, edges):
        raise RuntimeError("simulated expansion failure")

    monkeypatch.setattr(BomStore, "expand_all", boom)
    with pytest.raises(RuntimeError):
        store.apply_merge(preview["preview_id"])
    assert store.snapshot() == before
    assert store.get_merge_preview(preview["preview_id"])["applied_version"] is None
    assert store.get_merge_preview(preview["preview_id"])["applicable"] is True
    monkeypatch.undo()
    # The same confirmed preview can still be applied afterwards.
    applied = store.apply_merge(preview["preview_id"])
    assert applied["version_seq"] > before["version"]["version_seq"]
    assert ("B", "D") in _edges(store)


def test_apply_write_failure_rolls_back(store, monkeypatch):
    _commit(store, ("A", "B", 2))
    revision = store.create_offline_revision()
    preview = store.preview_merge(
        revision["revision_id"], parse_edges_csv("parent,child,qty\nA,B,5\n")
    )
    before = store.snapshot()

    real_insert = BomStore._insert_version

    def fail_after_version(conn, edges):
        real_insert(conn, edges)
        raise RuntimeError("simulated crash after version insert")

    monkeypatch.setattr(BomStore, "_insert_version", staticmethod(fail_after_version))
    with pytest.raises(RuntimeError):
        store.apply_merge(preview["preview_id"])
    assert store.snapshot() == before


# -- HTTP -----------------------------------------------------------------


def _client(tmp_path, name="merge"):
    store = BomStore(str(tmp_path / "http.db"))
    app = create_app(store, instance=name, demo_mode=True, seed_demo=False)
    return TestClient(app)


def test_http_download_upload_preview_apply_round_trip(tmp_path):
    with _client(tmp_path) as client:
        client.post(
            "/api/batch",
            json={"ops": [
                {"op": "upsert", "parent": "FG", "child": "SA", "qty": 2},
                {"op": "upsert", "parent": "SA", "child": "L1", "qty": 3},
            ]},
        )
        download = client.post("/api/offline/revisions")
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("text/csv")
        revision_id = download.headers["X-Bom-Revision"]
        assert revision_id
        assert download.text == "parent,child,qty\nFG,SA,2\nSA,L1,3\n"

        # Ordinary edit after the download.
        client.post(
            "/api/batch",
            json={"ops": [{"op": "upsert", "parent": "SA", "child": "L1", "qty": 5}]},
        )

        edited = "parent,child,qty\nFG,SA,2\nSA,L1,3\nSA,L2,4\n"
        preview_response = client.post(
            "/api/offline/preview",
            files={"file": ("bom.csv", edited.encode("utf-8"), "text/csv")},
            data={"revision_id": revision_id},
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["conflicts"] == []
        assert preview["base_version"] == int(download.headers["X-Bom-Version"])
        assert preview["current_version"] == preview["base_version"] + 1
        # SA->L1 web change is taken; SA->L2 comes from the upload.
        assert preview["counts"]["final_edges"] == 3

        applied = client.post(f"/api/offline/apply/{preview['preview_id']}")
        assert applied.status_code == 200, applied.text
        body = applied.json()
        assert body["version_seq"] == body["applied_version"]
        state = client.get("/api/state").json()
        edges = {(e["parent"], e["child"]): e["qty"] for e in state["edges"]}
        assert edges == {("FG", "SA"): 2, ("SA", "L1"): 5, ("SA", "L2"): 4}
        usage = {(r["top_part"], r["leaf_part"]): r["qty"] for r in state["leaf_usage"]}
        assert usage[("FG", "L1")] == 10
        assert usage[("FG", "L2")] == 8


def test_http_conflict_flow_and_stale_rejection(tmp_path):
    with _client(tmp_path) as client:
        client.post(
            "/api/batch",
            json={"ops": [{"op": "upsert", "parent": "A", "child": "B", "qty": 1}]},
        )
        revision_id = client.post("/api/offline/revisions").headers["X-Bom-Revision"]
        client.post(
            "/api/batch",
            json={"ops": [{"op": "upsert", "parent": "A", "child": "B", "qty": 3}]},
        )
        edited = "parent,child,qty\nA,B,2\n"
        preview = client.post(
            "/api/offline/preview",
            files={"file": ("bom.csv", edited.encode(), "text/csv")},
            data={"revision_id": revision_id},
        ).json()
        assert preview["resolved"] is False
        conflict = preview["conflicts"][0]
        assert (conflict["base"], conflict["upload"], conflict["current"]) == (1, 2, 3)

        # Applying with conflicts unresolved is refused (409, nothing written).
        refused = client.post(f"/api/offline/apply/{preview['preview_id']}")
        assert refused.status_code == 409
        assert refused.json()["code"] == "UnresolvedConflictError"

        resolved = client.post(
            "/api/offline/preview",
            files={"file": ("bom.csv", edited.encode(), "text/csv")},
            data={
                "revision_id": revision_id,
                "resolutions": '[{"parent":"A","child":"B","choice":"upload"}]',
            },
        )
        assert resolved.status_code == 200, resolved.text
        resolved_body = resolved.json()
        assert resolved_body["resolved"] is True

        # Another commit lands before confirmation -> stale, must re-preview.
        client.post(
            "/api/batch",
            json={"ops": [{"op": "upsert", "parent": "A", "child": "C", "qty": 7}]},
        )
        stale = client.post(f"/api/offline/apply/{resolved_body['preview_id']}")
        assert stale.status_code == 409
        assert stale.json()["code"] == "StalePreviewError"
        state = client.get("/api/state").json()
        edges = {(e["parent"], e["child"]): e["qty"] for e in state["edges"]}
        assert edges == {("A", "B"): 3, ("A", "C"): 7}  # newer edit untouched

        fresh = client.post(
            "/api/offline/preview",
            files={"file": ("bom.csv", edited.encode(), "text/csv")},
            data={
                "revision_id": revision_id,
                "resolutions": '[{"parent":"A","child":"B","choice":"upload"}]',
            },
        ).json()
        applied = client.post(f"/api/offline/apply/{fresh['preview_id']}")
        assert applied.status_code == 200, applied.text
        state = client.get("/api/state").json()
        edges = {(e["parent"], e["child"]): e["qty"] for e in state["edges"]}
        assert edges == {("A", "B"): 2, ("A", "C"): 7}


def test_http_cycle_shows_path_and_changes_nothing(tmp_path):
    with _client(tmp_path) as client:
        client.post(
            "/api/batch",
            json={"ops": [
                {"op": "upsert", "parent": "A", "child": "B", "qty": 1},
                {"op": "upsert", "parent": "B", "child": "C", "qty": 1},
            ]},
        )
        revision_id = client.post("/api/offline/revisions").headers["X-Bom-Revision"]
        before = client.get("/api/state").json()
        response = client.post(
            "/api/offline/preview",
            files={
                "file": (
                    "bom.csv",
                    b"parent,child,qty\nA,B,1\nB,C,1\nC,A,1\n",
                    "text/csv",
                )
            },
            data={"revision_id": revision_id},
        )
        assert response.status_code == 409
        assert response.json()["paths"] == [["A", "B", "C", "A"]]
        assert client.get("/api/state").json() == before


def test_http_list_revisions_reports_current_version(tmp_path):
    with _client(tmp_path) as client:
        client.post(
            "/api/batch",
            json={"ops": [{"op": "upsert", "parent": "A", "child": "B", "qty": 1}]},
        )
        revision_id = client.post("/api/offline/revisions").headers["X-Bom-Revision"]
        listing = client.get("/api/offline/revisions").json()
        assert listing["current_version"] == 2
        assert listing["revisions"][0]["revision_id"] == revision_id
        assert listing["revisions"][0]["edge_count"] == 1
        # The stored base can be re-downloaded byte for byte.
        again = client.get(f"/api/offline/revisions/{revision_id}.csv")
        assert again.status_code == 200
        assert again.text == "parent,child,qty\nA,B,1\n"
        assert again.headers["X-Bom-Revision"] == revision_id


def test_http_unknown_revision_and_bad_csv(tmp_path):
    with _client(tmp_path) as client:
        missing = client.post(
            "/api/offline/preview",
            files={"file": ("x.csv", b"parent,child,qty\n", "text/csv")},
            data={"revision_id": "nope"},
        )
        assert missing.status_code == 404
        revision_id = client.post("/api/offline/revisions").headers["X-Bom-Revision"]
        bad = client.post(
            "/api/offline/preview",
            files={"file": ("x.csv", b"parent,child\nA,B\n", "text/csv")},
            data={"revision_id": revision_id},
        )
        assert bad.status_code == 422


def test_offline_merge_available_on_second_web_app(tmp_path):
    """Two web apps share the DB: download on A, apply on B."""
    with _client(tmp_path, "alpha") as alpha, _client(tmp_path, "beta") as beta:
        alpha.post(
            "/api/batch",
            json={"ops": [{"op": "upsert", "parent": "A", "child": "B", "qty": 1}]},
        )
        revision_id = alpha.post("/api/offline/revisions").headers["X-Bom-Revision"]
        beta.post(
            "/api/batch",
            json={"ops": [{"op": "upsert", "parent": "A", "child": "C", "qty": 2}]},
        )
        preview = beta.post(
            "/api/offline/preview",
            files={
                "file": ("bom.csv", b"parent,child,qty\nA,B,1\nA,D,3\n", "text/csv")
            },
            data={"revision_id": revision_id},
        ).json()
        assert preview["conflicts"] == []
        applied = beta.post(f"/api/offline/apply/{preview['preview_id']}")
        assert applied.status_code == 200
        edges = {
            (e["parent"], e["child"]): e["qty"]
            for e in alpha.get("/api/state").json()["edges"]
        }
        assert edges == {("A", "B"): 1, ("A", "C"): 2, ("A", "D"): 3}


def test_apply_expansion_does_not_block_ordinary_edits(tmp_path):
    """During the merge apply's expansion another web app can still commit.

    The apply then loses its CAS (the library moved), so the merge is refused
    as stale rather than overwriting the concurrent edit - but the point here
    is that the ordinary editor was never blocked by the merge.
    """
    with _client(tmp_path, "alpha") as alpha, _client(tmp_path, "beta") as beta:
        alpha.post(
            "/api/batch",
            json={"ops": [{"op": "upsert", "parent": "A", "child": "B", "qty": 1}]},
        )
        revision_id = alpha.post("/api/offline/revisions").headers["X-Bom-Revision"]
        preview = alpha.post(
            "/api/offline/preview",
            files={
                "file": ("bom.csv", b"parent,child,qty\nA,B,1\nA,C,2\n", "text/csv")
            },
            data={"revision_id": revision_id},
        ).json()
        assert preview["applicable"] is True

        outcome = {}

        def slow_apply():
            outcome["response"] = alpha.post(
                f"/api/offline/apply/{preview['preview_id']}",
                headers={"X-Demo-Pause": "1"},
            )

        thread = threading.Thread(target=slow_apply)
        thread.start()
        time.sleep(0.2)  # alpha is now expanding, before BEGIN IMMEDIATE
        started = time.monotonic()
        response_b = beta.post(
            "/api/batch",
            json={"ops": [{"op": "upsert", "parent": "X", "child": "Y", "qty": 4}]},
        )
        elapsed = time.monotonic() - started
        thread.join(timeout=10)

        assert response_b.status_code == 200
        assert elapsed < 0.9, "merge apply held the write lock during expansion"
        assert outcome["response"].status_code == 409
        assert outcome["response"].json()["code"] == "StalePreviewError"
        edges = {
            (e["parent"], e["child"]): e["qty"]
            for e in alpha.get("/api/state").json()["edges"]
        }
        assert edges == {("A", "B"): 1, ("X", "Y"): 4}
        # The rejected merge changed nothing and must be re-previewed.
        fresh = alpha.post(
            "/api/offline/preview",
            files={
                "file": ("bom.csv", b"parent,child,qty\nA,B,1\nA,C,2\n", "text/csv")
            },
            data={"revision_id": revision_id},
        ).json()
        applied = alpha.post(f"/api/offline/apply/{fresh['preview_id']}")
        assert applied.status_code == 200, applied.text
        edges = {
            (e["parent"], e["child"]): e["qty"]
            for e in alpha.get("/api/state").json()["edges"]
        }
        assert edges == {("A", "B"): 1, ("A", "C"): 2, ("X", "Y"): 4}
