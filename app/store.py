"""SQLite storage for the BOM leaf-usage warehouse.

Design notes
------------
The SQLite database (WAL mode, ``busy_timeout``) is the single source of
truth and may be shared by several web processes.

Versioned model
    ``bom_version``    one row per committed structure version (BOM snapshot).
    ``bom_edge``       parent -> child relations, keyed by version.
    ``leaf_usage``     recursively expanded leaf usage, keyed by version.
    ``bom_head``       single row pointing at the currently published version.

A structural edit is one *batch*: it is applied in an ``IMMEDIATE``
transaction that writes both a brand-new ``bom_version`` (edges) and the
matching ``leaf_usage`` rows, then flips ``bom_head``.  Readers only ever
resolve ``bom_head`` inside their own read transaction, so they see one
complete version - never a mixture - and never trigger an expansion.
If anything fails the transaction rolls back and the previous version
stays published.

Concurrency safety without holding a write lock while expanding
---------------------------------------------------------------
Expansion (and cycle detection) runs on an in-memory snapshot of the batch's
final edges, *before* the write transaction starts, so no write lock is held
while it runs and other editors can commit meanwhile.  The final short
``BEGIN IMMEDIATE`` is a compare-and-swap on the published sequence number;
if another commit landed in between, it rolls back and re-plans from the
freshly published structure, re-running the cycle check over the combined
structure.  A stale plan can therefore neither overwrite newer structure,
drop newly added dependencies, nor let two individually acyclic edits
combine into a cycle.  Non-blocking lock attempts plus backoff keep two
web processes from serialising behind one stale plan.

Version-based storage also makes reads and downloads trivially consistent:
the API resolves the head once inside a single read transaction and only
selects already-materialised rows, so a response never mixes two commits.

Offline revisions and three-way merge
    ``offline_revision``  immutable snapshot (CSV) of the structure at the
                          moment the complete parent/child/qty file was
                          downloaded; it is the merge *base*.
    ``merge_preview``     frozen three-way comparison plus the resolved final
                          structure, and the published version it was
                          previewed against.  Applying requires every conflict
                          to be resolved and the published version to be
                          unchanged, otherwise the confirmation is rejected
                          (the user must preview again) so newer edits are
                          never overwritten.  The final structure is written
                          with edges + materialised usage in one transaction,
                          after the usual in-memory cycle check; a cycle
                          rejects the whole batch.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

MAX_PARTS = 100
MAX_EDGES = 200
MAX_QUANTITY = 1_000_000_000
MAX_TOTAL = 2 ** 63 - 1  # SQLite INTEGER is signed 64-bit
DEFAULT_DB_PATH = os.environ.get("BOM_DB_PATH", os.path.join("data", "bom.db"))
MAX_RETRIES = 25


class BomError(Exception):
    """Base class for domain errors surfaced as HTTP 4xx."""

    status_code = 400

    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.extra: dict[str, Any] = extra


class ValidationError(BomError):
    status_code = 422


class NotFoundError(BomError):
    status_code = 404


class CapacityError(BomError):
    status_code = 409


class ConflictError(BomError):
    status_code = 409


class StalePreviewError(ConflictError):
    """The database changed after a merge preview: the confirmation is void."""

    def __init__(self, current_version: int, preview_version: int):
        self.current_version = current_version
        self.preview_version = preview_version
        super().__init__(
            f"预览后当前库已更新为 v{current_version}（预览基于 v{preview_version}），"
            "旧确认已拒绝，请重新预览后再应用",
            current_version=current_version,
            preview_version=preview_version,
        )


class UnresolvedConflictError(ConflictError):
    def __init__(self, conflicts: list[dict[str, Any]]):
        self.conflicts = conflicts
        super().__init__(
            f"仍有 {len(conflicts)} 项冲突未解决，整批不得应用",
            conflicts=conflicts,
        )


class CyclicStructureError(BomError):
    status_code = 409

    def __init__(self, paths: list[list[str]]):
        self.paths = paths
        shown = "；".join(" → ".join(path) for path in paths[:5])
        super().__init__(f"检测到环路：{shown}")


@dataclass(frozen=True)
class Edge:
    parent: str
    child: str
    qty: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.parent, self.child)

    def as_dict(self) -> dict[str, Any]:
        return {"parent": self.parent, "child": self.child, "qty": self.qty}


@dataclass(frozen=True)
class BatchOp:
    """A single ordered batch operation (JSON body)."""

    op: str
    parent: str | None = None
    child: str | None = None
    qty: int | None = None
    upserts: tuple[Edge, ...] = ()
    deletes: tuple[tuple[str, str], ...] = ()


def _normalise_token(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是字符串")
    token = value.strip().upper()
    if not token:
        raise ValidationError(f"{field} 不能为空")
    if len(token) > 64:
        raise ValidationError(f"{field} 最长 64 个字符")
    if any(not (ch.isalnum() or ch in "-_.") for ch in token):
        raise ValidationError(f"{field} 只能包含字母、数字、- _ .")
    return token


def _normalise_qty(value: Any) -> int:
    if isinstance(value, bool):
        raise ValidationError("用量必须是正整数")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValidationError("用量必须是正整数")
        value = int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise ValidationError("用量必须是正整数")
        value = int(text)
    if not isinstance(value, int):
        raise ValidationError("用量必须是正整数")
    if value < 1:
        raise ValidationError("用量必须是正整数（>=1）")
    if value > MAX_QUANTITY:
        raise ValidationError(f"用量不能超过 {MAX_QUANTITY}")
    return value


def _edge_from_json(obj: Mapping[str, Any]) -> Edge:
    if not isinstance(obj, Mapping):
        raise ValidationError("关系条目必须是对象")
    return Edge(
        parent=_normalise_token(obj.get("parent"), "parent"),
        child=_normalise_token(obj.get("child"), "child"),
        qty=_normalise_qty(obj.get("qty")),
    )


def parse_ops(payload: Mapping[str, Any]) -> list[BatchOp]:
    reset_ops = parse_replace_reset(payload)
    if reset_ops is not None:
        return reset_ops
    ops = payload.get("ops")
    if not isinstance(ops, list) or not ops:
        raise ValidationError("ops 必须是非空数组")
    parsed: list[BatchOp] = []
    for index, raw in enumerate(ops, start=1):
        if not isinstance(raw, Mapping):
            raise ValidationError(f"第 {index} 条操作必须是对象")
        name = raw.get("op")
        if name == "upsert":
            parsed.append(BatchOp("upsert", upserts=(_edge_from_json(raw),)))
        elif name == "delete":
            parsed.append(
                BatchOp(
                    "delete",
                    parent=_normalise_token(raw.get("parent"), "parent"),
                    child=_normalise_token(raw.get("child"), "child"),
                )
            )
        elif name == "replace":
            raw_upserts = raw.get("upserts", [])
            raw_deletes = raw.get("deletes", [])
            if not isinstance(raw_upserts, list) or not isinstance(raw_deletes, list):
                raise ValidationError(f"第 {index} 条 replace 的 upserts/deletes 必须是数组")
            deletes: list[tuple[str, str]] = []
            for item in raw_deletes:
                if isinstance(item, Mapping):
                    deletes.append(
                        (
                            _normalise_token(item.get("parent"), "parent"),
                            _normalise_token(item.get("child"), "child"),
                        )
                    )
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    deletes.append(
                        (_normalise_token(item[0], "parent"), _normalise_token(item[1], "child"))
                    )
                else:
                    raise ValidationError(f"第 {index} 条 replace 的删除项格式错误")
            parsed.append(
                BatchOp(
                    "replace",
                    upserts=tuple(_edge_from_json(item) for item in raw_upserts),
                    deletes=tuple(deletes),
                )
            )
        else:
            raise ValidationError(f"第 {index} 条操作类型未知：{name!r}")
    return parsed


def apply_ops(base: Iterable[Edge], ops: Sequence[BatchOp]) -> tuple[list[Edge], list[tuple[str, str]]]:
    """Apply ordered ops to a base edge set.

    Returns the resulting edges (sorted) and the keys deleted during the
    batch (used to decide whether dependent usage rows must be refreshed).
    """
    edges: dict[tuple[str, str], Edge] = {edge.key: edge for edge in base}
    deleted: list[tuple[str, str]] = []
    for op in ops:
        if op.op == "upsert":
            for edge in op.upserts:
                edges[edge.key] = edge
        elif op.op == "delete":
            key = (op.parent, op.child)  # type: ignore[arg-type]
            edges.pop(key, None)
            deleted.append(key)
        elif op.op == "replace":
            if not op.deletes:
                # Whole-set replace: provided upserts become the full structure.
                deleted.extend(key for key in edges if key not in {e.key for e in op.upserts})
                edges = {}
            for key in op.deletes:
                edges.pop(key, None)
                deleted.append(key)
            for edge in op.upserts:
                edges[edge.key] = edge
        else:  # pragma: no cover - parse_ops rejects unknown ops
            raise ValidationError(f"未知操作 {op.op!r}")
    return sorted(edges.values(), key=lambda e: (e.parent, e.child)), deleted


def parts_from_edges(edges: Iterable[Edge]) -> list[str]:
    parts: set[str] = set()
    for edge in edges:
        parts.add(edge.parent)
        parts.add(edge.child)
    return sorted(parts)


def validate_limits(edges: Sequence[Edge], explicit_parts: Iterable[str] = ()) -> None:
    parts = set(parts_from_edges(edges)) | {p for p in explicit_parts}
    if len(parts) > MAX_PARTS:
        raise CapacityError(f"料号数量 {len(parts)} 超过上限 {MAX_PARTS}")
    if len(edges) > MAX_EDGES:
        raise CapacityError(f"父子关系数量 {len(edges)} 超过上限 {MAX_EDGES}")


def apply_replace(base: Iterable[Edge], op: BatchOp) -> list[Edge]:
    """Throw away the whole current structure in favour of ``upserts``."""
    del base  # ``op.deletes`` is only informational for a whole-set replace
    return sorted(
        {edge.key: edge for edge in op.upserts}.values(), key=lambda e: (e.parent, e.child)
    )


def parse_replace_reset(payload: Mapping[str, Any]) -> list[BatchOp] | None:
    """Detect the explicit ``{"reset": true, ...}`` batch form."""
    if not payload.get("reset"):
        return None
    raw_upserts = payload.get("upserts", [])
    if not isinstance(raw_upserts, list):
        raise ValidationError("upserts 必须是数组")
    upserts = tuple(_edge_from_json(item) for item in raw_upserts)
    if not upserts:
        raise ValidationError("reset 批次必须至少包含一条关系")
    return [BatchOp("replace", upserts=upserts)]


# --------------------------------------------------------------------------
# Graph algorithms (pure, run on snapshots - never under a DB write lock)
# --------------------------------------------------------------------------


def find_cycles(edges: Sequence[Edge], limit: int = 10) -> list[list[str]]:
    """Return up to ``limit`` distinct simple cycles as node paths.

    Each path starts and ends with the same part, e.g.
    ``["A", "B", "C", "A"]``.  Iterative DFS with a colour map keeps this
    O(V+E) and immune to recursion depth issues.
    """
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.parent, []).append(edge.child)
    for children in adjacency.values():
        children.sort()

    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {}
    stack: list[str] = []
    position: dict[str, int] = {}
    cycles: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def record(cycle: list[str]) -> bool:
        # Canonical rotation so A→B→A and B→A→B count as the same loop.
        body = cycle[:-1]
        rotations = [tuple(body[i:] + body[:i]) for i in range(len(body))]
        canonical = min(rotations)
        if canonical in seen:
            return False
        seen.add(canonical)
        cycles.append(cycle)
        return len(cycles) >= limit

    for root in sorted(adjacency):
        if colour.get(root, WHITE) != WHITE:
            continue
        # Explicit DFS frames: (node, iterator index)
        frames: list[tuple[str, int]] = [(root, 0)]
        colour[root] = GREY
        stack.append(root)
        position[root] = 0
        while frames:
            node, index = frames[-1]
            children = adjacency.get(node, [])
            if index >= len(children):
                frames.pop()
                colour[node] = BLACK
                stack.pop()
                position.pop(node, None)
                continue
            child = children[index]
            frames[-1] = (node, index + 1)
            state = colour.get(child, WHITE)
            if state == GREY:
                start = position[child]
                if start is not None:
                    if record(stack[start:] + [child]):
                        return cycles
            elif state == WHITE:
                colour[child] = GREY
                position[child] = len(stack)
                stack.append(child)
                frames.append((child, 0))
    return cycles


def _edge_signature(edges: Iterable[Edge]) -> tuple[tuple[str, str, int], ...]:
    return tuple((e.parent, e.child, e.qty) for e in edges)


# --------------------------------------------------------------------------
# Offline revision CSV + three-way merge (pure helpers)
# --------------------------------------------------------------------------

OFFLINE_CSV_COLUMNS = ("parent", "child", "qty")
MAX_OFFLINE_CSV_BYTES = 5 * 1024 * 1024
OFFLINE_KEEP_REVISIONS = 200
OFFLINE_KEEP_PREVIEWS = 100


def edges_csv(edges: Sequence[Edge]) -> str:
    """Complete ``parent,child,qty`` CSV used as the offline editing file."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(OFFLINE_CSV_COLUMNS)
    for edge in sorted(edges, key=lambda e: (e.parent, e.child)):
        writer.writerow([edge.parent, edge.child, edge.qty])
    return buffer.getvalue()


def parse_edges_csv(text: str) -> list[Edge]:
    """Parse a complete CSV. Missing rows mean deletion (whole-file replace)."""
    if not isinstance(text, str):
        raise ValidationError("CSV 内容必须是文本")
    if len(text.encode("utf-8")) > MAX_OFFLINE_CSV_BYTES:
        raise ValidationError("CSV 文件过大（上限 5 MB）")
    text = text.lstrip("﻿")
    if not text.strip():
        raise ValidationError("CSV 为空；完整文件至少需要表头 parent,child,qty")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:  # pragma: no cover - guarded by the empty check
        raise ValidationError("CSV 为空") from None
    names = [cell.strip().lower() for cell in header]
    for column in OFFLINE_CSV_COLUMNS:
        if column not in names:
            raise ValidationError(f"CSV 缺少列 {column}（需要 parent,child,qty 表头）")
    if len(set(names)) != len(names):
        raise ValidationError("CSV 表头存在重复列")
    index = {name: names.index(name) for name in OFFLINE_CSV_COLUMNS}
    edges: dict[tuple[str, str], Edge] = {}
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) < len(names):
            row = row + [""] * (len(names) - len(row))
        parent = _normalise_token(row[index["parent"]], f"第 {line_no} 行 parent")
        child = _normalise_token(row[index["child"]], f"第 {line_no} 行 child")
        qty = _normalise_qty(row[index["qty"]])
        key = (parent, child)
        if key in edges:
            raise ValidationError(f"第 {line_no} 行父子关系 {parent} → {child} 重复出现")
        edges[key] = Edge(parent, child, qty)
    return sorted(edges.values(), key=lambda e: (e.parent, e.child))


@dataclass(frozen=True)
class MergeChange:
    """One parent/child relation compared across base, upload and current."""

    parent: str
    child: str
    base: int | None
    upload: int | None
    current: int | None
    decision: str  # merged | upload | current | conflict
    result: int | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.parent, self.child)

    @property
    def kind(self) -> str:
        if self.decision == "conflict":
            return "conflict"
        if self.base is None and self.result is not None:
            return "added"
        if self.base is not None and self.result is None:
            return "deleted"
        if self.decision == "merged":
            return "merged"
        return "changed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "parent": self.parent,
            "child": self.child,
            "base": self.base,
            "upload": self.upload,
            "current": self.current,
            "decision": self.decision,
            "result": self.result,
            "kind": self.kind,
        }


def _edge_map(edges: Iterable[Edge]) -> dict[tuple[str, str], int]:
    return {edge.key: edge.qty for edge in edges}


def three_way_merge(
    base: Iterable[Edge], upload: Iterable[Edge], current: Iterable[Edge]
) -> tuple[list[MergeChange], list[MergeChange]]:
    """Compare by parent/child relation.

    * only one side changed  -> take that side
    * both sides agree       -> merged value
    * both changed differently (including delete vs value) -> conflict

    Rows identical on all three sides produce no entry.  ``base`` is the
    immutable offline revision snapshot, ``upload`` the edited complete CSV
    and ``current`` the published structure when the merge is previewed.
    """
    base_map = _edge_map(base)
    upload_map = _edge_map(upload)
    current_map = _edge_map(current)
    keys = sorted(set(base_map) | set(upload_map) | set(current_map))
    changes: list[MergeChange] = []
    conflicts: list[MergeChange] = []
    for key in keys:
        before = base_map.get(key)
        edited = upload_map.get(key)
        now = current_map.get(key)
        if edited == now:
            decision, result = "merged", edited
        elif edited == before:
            decision, result = "current", now
        elif now == before:
            decision, result = "upload", edited
        else:
            decision, result = "conflict", None
        if decision == "conflict":
            conflicts.append(MergeChange(key[0], key[1], before, edited, now, decision, None))
        elif edited != before or now != before:
            changes.append(MergeChange(key[0], key[1], before, edited, now, decision, result))
    return changes, conflicts


def parse_resolutions(payload: Any) -> dict[tuple[str, str], str]:
    """Parse ``[{"parent","child","choice":"current"|"upload"}]``."""
    if payload is None:
        return {}
    if not isinstance(payload, list):
        raise ValidationError("resolutions 必须是数组")
    parsed: dict[tuple[str, str], str] = {}
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, Mapping):
            raise ValidationError(f"第 {index} 条冲突选择必须是对象")
        parent = _normalise_token(item.get("parent"), f"第 {index} 条 parent")
        child = _normalise_token(item.get("child"), f"第 {index} 条 child")
        choice = item.get("choice")
        if choice not in ("current", "upload"):
            raise ValidationError(f"第 {index} 条 choice 必须是 current 或 upload")
        key = (parent, child)
        if key in parsed:
            raise ValidationError(f"冲突 {parent} → {child} 被选择了两次")
        parsed[key] = choice
    return parsed


def resolve_merge(
    base: Iterable[Edge],
    upload: Iterable[Edge],
    current: Iterable[Edge],
    resolutions: Mapping[tuple[str, str], str],
    *,
    allow_unresolved: bool = False,
) -> tuple[list[Edge], list[MergeChange], list[MergeChange]]:
    """Build the final structure; refuse when any conflict is unresolved.

    ``allow_unresolved`` is for *previewing*: unresolved rows keep their
    current value so the comparison can be shown, but the preview is not
    applicable.  Applying always requires every conflict to be resolved.
    Resolutions for rows that are no longer conflicting (for example after
    the library changed) are ignored, so a re-preview never dead-ends.
    """
    changes, conflicts = three_way_merge(base, upload, current)
    unresolved = [item.as_dict() for item in conflicts if item.key not in resolutions]
    if unresolved and not allow_unresolved:
        raise UnresolvedConflictError(unresolved)

    final: dict[tuple[str, str], Edge] = {edge.key: edge for edge in current}
    for change in changes:
        if change.result is None:
            final.pop(change.key, None)
        else:
            final[change.key] = Edge(change.parent, change.child, change.result)
    for conflict in conflicts:
        choice = resolutions.get(conflict.key)
        if choice is None:  # preview only: leave the current value in place
            continue
        value = conflict.current if choice == "current" else conflict.upload
        if value is None:
            final.pop(conflict.key, None)
        else:
            final[conflict.key] = Edge(conflict.parent, conflict.child, value)
    final_edges = sorted(final.values(), key=lambda e: (e.parent, e.child))
    return final_edges, changes, conflicts


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


@dataclass
class CommitResult:
    version_id: int
    version_seq: int
    created_at: str
    reused: bool
    retries: int
    leaf_rows: int


class BomStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        directory = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(directory, exist_ok=True)
        self._init_schema()

    # -- connection helpers ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,  # explicit transaction control
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # WAL is a persistent database property, set once by _init_schema; it
        # must not be re-negotiated per request (that can need extra locks).
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        """Consistent read snapshot: head is resolved once inside a txn."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def writer(self) -> Iterator[sqlite3.Connection]:
        """Short write transaction (``BEGIN IMMEDIATE``).

        Only used for small bookkeeping writes (offline revisions, preview
        bookkeeping); never held while expanding or comparing structures.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bom_version (
                    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at  TEXT NOT NULL,
                    edge_count  INTEGER NOT NULL,
                    part_count  INTEGER NOT NULL,
                    checksum    TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bom_edge (
                    version_seq INTEGER NOT NULL REFERENCES bom_version(seq),
                    parent      TEXT NOT NULL,
                    child       TEXT NOT NULL,
                    qty         INTEGER NOT NULL CHECK (qty > 0),
                    PRIMARY KEY (version_seq, parent, child)
                );

                CREATE TABLE IF NOT EXISTS leaf_usage (
                    version_seq INTEGER NOT NULL REFERENCES bom_version(seq),
                    top_part    TEXT NOT NULL,
                    leaf_part   TEXT NOT NULL,
                    qty         INTEGER NOT NULL CHECK (qty > 0),
                    PRIMARY KEY (version_seq, top_part, leaf_part)
                ) WITHOUT ROWID;

                CREATE INDEX IF NOT EXISTS ix_leaf_usage_leaf
                    ON leaf_usage (version_seq, leaf_part);

                CREATE TABLE IF NOT EXISTS bom_head (
                    id          INTEGER PRIMARY KEY CHECK (id = 1),
                    version_seq INTEGER NOT NULL REFERENCES bom_version(seq),
                    updated_at  TEXT NOT NULL
                );

                -- Immutable offline-edit bases.  Created when the complete
                -- parent/child/qty CSV is downloaded; never updated.
                CREATE TABLE IF NOT EXISTS offline_revision (
                    revision_id TEXT PRIMARY KEY,
                    version_seq INTEGER NOT NULL,
                    created_at  TEXT NOT NULL,
                    edge_count  INTEGER NOT NULL,
                    checksum    TEXT NOT NULL,
                    csv         TEXT NOT NULL
                );

                -- Frozen three-way comparison + resolved final structure.
                -- Applying it is only allowed while the published version is
                -- still ``current_version`` (otherwise: stale preview).
                CREATE TABLE IF NOT EXISTS merge_preview (
                    preview_id      TEXT PRIMARY KEY,
                    revision_id     TEXT NOT NULL,
                    base_version    INTEGER NOT NULL,
                    current_version INTEGER NOT NULL,
                    created_at      TEXT NOT NULL,
                    resolved        INTEGER NOT NULL,
                    changes         TEXT NOT NULL,
                    conflicts       TEXT NOT NULL,
                    resolutions     TEXT NOT NULL,
                    final_edges     TEXT NOT NULL,
                    applied_version INTEGER,
                    applied_at      TEXT
                );
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT version_seq FROM bom_head WHERE id = 1").fetchone()
            if row is None:
                seq = self._insert_empty_version(conn)
                conn.execute(
                    "INSERT INTO bom_head (id, version_seq, updated_at) VALUES (1, ?, ?)",
                    (seq, _now()),
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # -- low-level snapshot helpers ---------------------------------------

    @staticmethod
    def _load_edges(conn: sqlite3.Connection, version_seq: int) -> list[Edge]:
        rows = conn.execute(
            "SELECT parent, child, qty FROM bom_edge WHERE version_seq = ? ORDER BY parent, child",
            (version_seq,),
        ).fetchall()
        return [Edge(r["parent"], r["child"], r["qty"]) for r in rows]

    @staticmethod
    def _insert_empty_version(conn: sqlite3.Connection) -> int:
        cursor = conn.execute(
            "INSERT INTO bom_version (created_at, edge_count, part_count, checksum) VALUES (?, 0, 0, ?)",
            (_now(), _checksum([])),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _insert_version(conn: sqlite3.Connection, edges: Sequence[Edge]) -> int:
        cursor = conn.execute(
            "INSERT INTO bom_version (created_at, edge_count, part_count, checksum) VALUES (?, ?, ?, ?)",
            (_now(), len(edges), len(set(parts_from_edges(edges))), _checksum(edges)),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _prune(conn: sqlite3.Connection, keep_seq: int) -> None:
        """Keep only the published version; results must persist across restarts."""
        conn.execute("DELETE FROM leaf_usage WHERE version_seq <> ?", (keep_seq,))
        conn.execute("DELETE FROM bom_edge WHERE version_seq <> ?", (keep_seq,))
        conn.execute("DELETE FROM bom_version WHERE seq <> ?", (keep_seq,))

    # -- expansion ---------------------------------------------------------

    def expand(self, edges: Sequence[Edge], top_part: str) -> dict[str, int]:
        """Multiply quantities down every path and sum across paths.

        Dynamic programming over the DAG (memoised per sub-assembly) keeps
        shared sub-assemblies from exploding into an exponential number of
        paths.  A part with no children is a leaf; the top part itself is
        not reported.  Cycles must already have been rejected; a leftover
        cycle raises ``CyclicStructureError`` defensively.
        """
        children: dict[str, list[tuple[str, int]]] = {}
        for edge in edges:
            children.setdefault(edge.parent, []).append((edge.child, edge.qty))
        memo: dict[str, dict[str, int]] = {}
        visiting: list[str] = []

        def leaves_of(part: str) -> dict[str, int]:
            cached = memo.get(part)
            if cached is not None:
                return cached
            if part in visiting:
                start = visiting.index(part)
                raise CyclicStructureError([visiting[start:] + [part]])
            kids = children.get(part)
            if not kids:
                result = {part: 1}
            else:
                visiting.append(part)
                result = {}
                for child, qty in kids:
                    for leaf, multiplier in leaves_of(child).items():
                        result[leaf] = result.get(leaf, 0) + multiplier * qty
                visiting.pop()
            memo[part] = result
            return result

        totals = dict(leaves_of(top_part))
        totals.pop(top_part, None)
        return totals

    def expand_all(self, edges: Sequence[Edge]) -> dict[tuple[str, str], int]:
        """Leaf usage for every assembly (every part that has children).

        The warehouse stores one row per (assembly, leaf), so a sub-assembly
        shared by two top items is expanded once for itself and again as part
        of each parent's own expansion.
        """
        assemblies = sorted({edge.parent for edge in edges})
        usage: dict[tuple[str, str], int] = {}
        for top in assemblies:
            for leaf, qty in self.expand(edges, top).items():
                if qty > MAX_TOTAL:
                    raise OverflowError("展开用量超过 64 位整数上限")
                usage[(top, leaf)] = qty
        return usage

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _head(conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT h.version_seq, h.updated_at, v.created_at, v.edge_count, v.part_count, v.checksum
            FROM bom_head h JOIN bom_version v ON v.seq = h.version_seq
            WHERE h.id = 1
            """
        ).fetchone()
        if row is None:  # pragma: no cover - schema bootstrap guarantees a row
            raise RuntimeError("bom_head 未初始化")
        return row

    @staticmethod
    def _read_head(conn: sqlite3.Connection) -> sqlite3.Row:
        """Static alias so ``_claim_writer`` (also static) can read the head."""
        return BomStore._head(conn)

    @staticmethod
    def _claim_writer(conn: sqlite3.Connection, base_seq: int) -> bool:
        """Take the single-writer slot and CAS on the published version.

        ``BEGIN IMMEDIATE`` acquires SQLite's one write lock.  It is held only
        for the short pure-SQL work below; no expansion or cycle check ever
        runs inside it, so other web processes are not blocked while a batch
        is being planned.  If another commit landed since the snapshot, the
        caller rolls back and re-plans against the fresh structure.
        """
        conn.execute("BEGIN IMMEDIATE")
        head = BomStore._read_head(conn)
        if head["version_seq"] != base_seq:
            conn.execute("ROLLBACK")
            return False
        return True

    def snapshot(self) -> dict[str, Any]:
        with self.reader() as conn:
            head = self._head(conn)
            edges = self._load_edges(conn, head["version_seq"])
            usage = conn.execute(
                """
                SELECT top_part, leaf_part, qty FROM leaf_usage
                WHERE version_seq = ? ORDER BY top_part, leaf_part
                """,
                (head["version_seq"],),
            ).fetchall()
            return {
                "version": dict(head),
                "edges": [e.as_dict() for e in edges],
                "leaf_usage": [dict(row) for row in usage],
            }

    def parts(self) -> list[str]:
        with self.reader() as conn:
            head = self._head(conn)
            return parts_from_edges(self._load_edges(conn, head["version_seq"]))

    def usage_rows(
        self, top: str | None = None, leaf: str | None = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Read leaf usage for the published version inside one snapshot."""
        top = top.strip().upper() if top else None
        leaf = leaf.strip().upper() if leaf else None
        with self.reader() as conn:
            head = self._head(conn)
            sql = [
                "SELECT top_part, leaf_part, qty FROM leaf_usage WHERE version_seq = ?",
            ]
            params: list[Any] = [head["version_seq"]]
            if top:
                sql.append("AND top_part = ?")
                params.append(top)
            if leaf:
                sql.append("AND leaf_part = ?")
                params.append(leaf)
            sql.append("ORDER BY top_part, leaf_part")
            rows = conn.execute(" ".join(sql), params).fetchall()
            return dict(head), [dict(row) for row in rows]

    def usage_csv(self, top: str | None = None, leaf: str | None = None) -> tuple[str, int]:
        head, rows = self.usage_rows(top=top, leaf=leaf)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["top_part", "leaf_part", "qty", "version_seq"])
        for row in rows:
            writer.writerow([row["top_part"], row["leaf_part"], row["qty"], head["version_seq"]])
        return buffer.getvalue(), int(head["version_seq"])

    # -- writes ------------------------------------------------------------

    def commit_batch(
        self,
        ops: Sequence[BatchOp],
        pause_hook: Callable[[int, int], None] | None = None,
    ) -> CommitResult:
        """Apply ops atomically; retry with re-planning on concurrent commits.

        The expansion (expensive) happens outside the write transaction.
        The write transaction re-reads the published snapshot under
        ``BEGIN IMMEDIATE`` and compare-and-swaps on it, so a stale plan is
        discarded and rebuilt from the freshest structure.

        ``pause_hook`` (demo-only) runs *before* the write transaction with
        ``(attempt, base_seq)`` so tests can interleave a second writer while
        the first one is still expanding - proving no write lock is held.
        """
        if not ops:
            raise ValidationError("ops 不能为空")
        last_error: Exception | None = None
        jitter = (hash((ops[0].op, len(ops), ops[-1].parent)) % 100) / 1000
        for attempt in range(MAX_RETRIES):
            with self.reader() as conn:
                head = self._head(conn)
                base_seq = head["version_seq"]
                base_edges = self._load_edges(conn, base_seq)
            result = self._try_commit(ops, base_seq, base_edges, attempt, pause_hook)
            if isinstance(result, CommitResult):
                if attempt:
                    result.retries = attempt
                return result
            last_error = result
            # Back off with jitter to de-synchronise two writers.
            time.sleep(min(0.005 * (2 ** attempt), 0.2) + jitter * attempt)
        raise ConflictError(f"并发修改过于频繁，请重试（{last_error}）")

    def _try_commit(
        self,
        ops: Sequence[BatchOp],
        base_seq: int,
        base_edges: Sequence[Edge],
        attempt: int = 0,
        pause_hook: Callable[[int, int], None] | None = None,
    ) -> CommitResult | None:
        new_edges, _deleted = apply_ops(base_edges, ops)
        validate_limits(new_edges)
        cycles = find_cycles(new_edges)
        if cycles:
            raise CyclicStructureError(cycles)

        # Pure computation on the snapshot: no write lock is held here.
        if pause_hook is not None:
            pause_hook(attempt, base_seq)
        try:
            usage = self.expand_all(new_edges)
        except OverflowError as exc:
            raise ValidationError(f"用量溢出：{exc}") from None

        conn = self._connect()
        try:
            if not self._claim_writer(conn, base_seq):
                return None  # concurrent commit: re-plan on fresh data
            head = self._head(conn)
            current_edges = self._load_edges(conn, base_seq)
            if _edge_signature(current_edges) != _edge_signature(base_edges):  # pragma: no cover
                conn.execute("ROLLBACK")
                return None
            if _edge_signature(new_edges) == _edge_signature(current_edges):
                # No-op batch: keep the existing version untouched.
                conn.execute("COMMIT")
                return CommitResult(base_seq, base_seq, head["created_at"], True, 0, 0)

            new_seq = self._insert_version(conn, new_edges)
            conn.executemany(
                "INSERT INTO bom_edge (version_seq, parent, child, qty) VALUES (?, ?, ?, ?)",
                [(new_seq, e.parent, e.child, e.qty) for e in new_edges],
            )
            conn.executemany(
                "INSERT INTO leaf_usage (version_seq, top_part, leaf_part, qty) VALUES (?, ?, ?, ?)",
                [(new_seq, top, leaf, qty) for (top, leaf), qty in sorted(usage.items())],
            )
            conn.execute(
                "UPDATE bom_head SET version_seq = ?, updated_at = ? WHERE id = 1",
                (new_seq, _now()),
            )
            # Only the newly published version's rows survive (restart-safe,
            # bounded storage) - done in the same transaction so readers can
            # never observe a half-pruned snapshot.
            self._prune(conn, new_seq)
            created_row = conn.execute(
                "SELECT created_at FROM bom_version WHERE seq = ?", (new_seq,)
            ).fetchone()
            conn.execute("COMMIT")
            return CommitResult(
                new_seq, new_seq, created_row["created_at"], False, 0, len(usage)
            )
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def seed_if_empty(self, edges: Sequence[Edge]) -> bool:
        """Insert demo structure only when the warehouse is empty."""
        with self.reader() as conn:
            head = self._head(conn)
            if conn.execute(
                "SELECT 1 FROM bom_edge WHERE version_seq = ? LIMIT 1", (head["version_seq"],)
            ).fetchone():
                return False
        ops = [BatchOp("upsert", upserts=(edge,)) for edge in edges]
        self.commit_batch(ops)
        return True

    # -- offline revisions + three-way merge -------------------------------

    def create_offline_revision(self) -> dict[str, Any]:
        """Snapshot the published structure as an immutable download base.

        The base is read under one read snapshot and written in a separate
        short transaction.  If a commit lands in between, the recorded
        version is the one actually snapshotted, not a newer head - callers
        can still compare it with the current head and notice the difference.
        """
        revision_id = secrets.token_urlsafe(12)
        with self.reader() as conn:
            head = self._head(conn)
            version_seq = int(head["version_seq"])
            edges = self._load_edges(conn, version_seq)
            created_at = _now()
            body = edges_csv(edges)
        with self.writer() as conn:
            conn.execute(
                "INSERT INTO offline_revision "
                "(revision_id, version_seq, created_at, edge_count, checksum, csv) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (revision_id, version_seq, created_at, len(edges),
                 hashlib.sha256(body.encode("utf-8")).hexdigest(), body),
            )
            # Keep the bookkeeping bounded; never prune a revision that still
            # has an unapplied preview.
            conn.execute(
                """
                DELETE FROM offline_revision
                WHERE revision_id NOT IN (
                    SELECT revision_id FROM offline_revision
                    ORDER BY created_at DESC, rowid DESC LIMIT ?
                )
                AND revision_id NOT IN (
                    SELECT revision_id FROM merge_preview WHERE applied_version IS NULL
                )
                """,
                (OFFLINE_KEEP_REVISIONS,),
            )
        return {
            "revision_id": revision_id,
            "version_seq": version_seq,
            "created_at": created_at,
            "edge_count": len(edges),
            "csv": body,
        }

    def get_offline_revision(self, revision_id: str) -> dict[str, Any]:
        with self.reader() as conn:
            row = conn.execute(
                "SELECT * FROM offline_revision WHERE revision_id = ?", (revision_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"修订单 {revision_id} 不存在")
        return dict(row)

    def offline_revision_csv(self, revision_id: str) -> tuple[str, dict[str, Any]]:
        row = self.get_offline_revision(revision_id)
        return row["csv"], row

    def list_offline_revisions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.reader() as conn:
            rows = conn.execute(
                "SELECT revision_id, version_seq, created_at, edge_count, checksum "
                "FROM offline_revision ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _load_current_edges(self, conn: sqlite3.Connection) -> tuple[int, list[Edge]]:
        head = self._head(conn)
        return int(head["version_seq"]), self._load_edges(conn, head["version_seq"])

    def preview_merge(
        self,
        revision_id: str,
        upload: Sequence[Edge],
        resolutions: Mapping[tuple[str, str], str] | None = None,
    ) -> dict[str, Any]:
        """Freeze a three-way comparison against the current structure.

        This takes only a read snapshot (no write lock; expansion is not run
        here) and stores the frozen comparison so that applying it can later
        be rejected if the published version moved on.  All conflicts must be
        resolved for the preview to be applicable.
        """
        resolutions = dict(resolutions or {})
        revision = self.get_offline_revision(revision_id)
        base_edges = parse_edges_csv(revision["csv"])
        with self.reader() as conn:
            current_version, current_edges = self._load_current_edges(conn)
            final_edges, changes, conflicts = resolve_merge(
                base_edges, upload, current_edges, resolutions, allow_unresolved=True
            )
            validate_limits(final_edges)
            cycles = find_cycles(final_edges)
            if cycles:
                raise CyclicStructureError(cycles)
            resolved = all(item.key in resolutions for item in conflicts)
            preview_id = secrets.token_urlsafe(12)
            payload = {
                "preview_id": preview_id,
                "revision_id": revision_id,
                "base_version": revision["version_seq"],
                "current_version": current_version,
                "resolved": resolved,
                "applicable": resolved,
                "changes": [item.as_dict() for item in changes],
                "conflicts": [item.as_dict() for item in conflicts],
                "final_edges": [e.as_dict() for e in final_edges],
                "counts": _merge_counts(changes, conflicts, final_edges),
            }
        # Bookkeeping write outside the read snapshot (short BEGIN IMMEDIATE).
        with self.writer() as conn:
            conn.execute(
                "INSERT INTO merge_preview (preview_id, revision_id, base_version, "
                "current_version, created_at, resolved, changes, conflicts, resolutions, "
                "final_edges) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    preview_id,
                    revision_id,
                    revision["version_seq"],
                    current_version,
                    _now(),
                    1 if resolved else 0,
                    json.dumps(payload["changes"], ensure_ascii=False),
                    json.dumps(payload["conflicts"], ensure_ascii=False),
                    json.dumps(
                        [{"parent": p, "child": c, "choice": choice}
                         for (p, c), choice in sorted(resolutions.items())],
                        ensure_ascii=False,
                    ),
                    json.dumps(payload["final_edges"], ensure_ascii=False),
                ),
            )
            # Bounded bookkeeping; applied previews are kept (they are the
            # audit trail of what was applied) while old unapplied ones are
            # dropped together with their revision.
            conn.execute(
                """
                DELETE FROM merge_preview
                WHERE preview_id IN (
                    SELECT preview_id FROM merge_preview
                    WHERE applied_version IS NULL
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (OFFLINE_KEEP_PREVIEWS,),
            )
            conn.execute(
                """
                DELETE FROM offline_revision
                WHERE revision_id NOT IN (
                    SELECT revision_id FROM merge_preview WHERE applied_version IS NULL
                )
                  AND revision_id NOT IN (
                      SELECT revision_id FROM offline_revision
                      ORDER BY created_at DESC, rowid DESC LIMIT ?
                  )
                """,
                (OFFLINE_KEEP_REVISIONS,),
            )
        return payload

    def get_merge_preview(self, preview_id: str) -> dict[str, Any]:
        with self.reader() as conn:
            row = conn.execute(
                "SELECT * FROM merge_preview WHERE preview_id = ?", (preview_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"合并预览 {preview_id} 不存在")
        return self._preview_payload(row)

    @staticmethod
    def _preview_payload(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        changes = json.loads(data["changes"])
        conflicts = json.loads(data["conflicts"])
        final_edges = json.loads(data["final_edges"])
        return {
            "preview_id": data["preview_id"],
            "revision_id": data["revision_id"],
            "base_version": data["base_version"],
            "current_version": data["current_version"],
            "created_at": data["created_at"],
            "resolved": bool(data["resolved"]),
            "applicable": bool(data["resolved"]) and data["applied_version"] is None,
            "applied_version": data["applied_version"],
            "applied_at": data["applied_at"],
            "changes": changes,
            "conflicts": conflicts,
            "final_edges": final_edges,
            "counts": _merge_counts(
                [_change_obj(item) for item in changes],
                [_change_obj(item) for item in conflicts],
                [Edge(e["parent"], e["child"], e["qty"]) for e in final_edges],
            ),
        }

    def apply_merge(
        self,
        preview_id: str,
        pause_hook: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Apply a confirmed preview: structure + materialised usage together.

        Strict concurrency rule: the preview is only applied while the
        published version is exactly the one it was previewed against.  If any
        commit landed in between, the confirmation is rejected
        (``StalePreviewError``) and the user must preview again - the newer
        edit is never overwritten.  The check and the write happen under the
        same ``BEGIN IMMEDIATE``/CAS, so the window cannot be reopened.

        Expansion runs outside the write lock (``pause_hook`` lets tests and
        the demo hold that window open), so ordinary edits stay available.
        """
        preview = self.get_merge_preview(preview_id)
        if preview["applied_version"] is not None:
            raise ConflictError(
                f"该预览已应用于 v{preview['applied_version']}，不能重复应用"
            )
        if not preview["resolved"]:
            raise UnresolvedConflictError(preview["conflicts"])
        final_edges = [
            Edge(item["parent"], item["child"], int(item["qty"]))
            for item in preview["final_edges"]
        ]
        expected = int(preview["current_version"])
        current = self.published_version()
        if current != expected:
            raise StalePreviewError(current, expected)
        result = self._commit_exact(final_edges, expected, pause_hook)
        with self.writer() as conn:
            conn.execute(
                "UPDATE merge_preview SET applied_version = ?, applied_at = ? "
                "WHERE preview_id = ?",
                (result.version_id, _now(), preview_id),
            )
        return {
            **self.get_merge_preview(preview_id),
            "version_id": result.version_id,
            "version_seq": result.version_seq,
            "retries": 0,
            "leaf_rows": result.leaf_rows,
            "reused": result.reused,
        }

    def _commit_exact(
        self,
        edges: Sequence[Edge],
        expected_seq: int,
        pause_hook: Callable[[int, int], None] | None = None,
    ) -> CommitResult:
        """Publish exactly ``edges`` iff ``expected_seq`` is still published.

        Same atomic shape as an ordinary batch (cycle check, expansion outside
        the write lock, then one ``BEGIN IMMEDIATE`` writing edges + usage +
        head), but with a strict CAS: no re-planning, so a concurrent commit
        causes rejection instead of being overwritten.
        """
        ordered = sorted(edges, key=lambda e: (e.parent, e.child))
        validate_limits(ordered)
        cycles = find_cycles(ordered)
        if cycles:
            raise CyclicStructureError(cycles)
        if pause_hook is not None:
            pause_hook(0, expected_seq)
        try:
            usage = self.expand_all(ordered)
        except OverflowError as exc:
            raise ValidationError(f"用量溢出：{exc}") from None
        conn = self._connect()
        try:
            if not self._claim_writer(conn, expected_seq):
                current = self.published_version()
                raise StalePreviewError(current, expected_seq)
            head = self._head(conn)
            current_edges = self._load_edges(conn, expected_seq)
            if _edge_signature(ordered) == _edge_signature(current_edges):
                conn.execute("COMMIT")
                return CommitResult(
                    expected_seq, expected_seq, head["created_at"], True, 0, 0
                )
            new_seq = self._insert_version(conn, ordered)
            conn.executemany(
                "INSERT INTO bom_edge (version_seq, parent, child, qty) VALUES (?, ?, ?, ?)",
                [(new_seq, e.parent, e.child, e.qty) for e in ordered],
            )
            conn.executemany(
                "INSERT INTO leaf_usage (version_seq, top_part, leaf_part, qty) VALUES (?, ?, ?, ?)",
                [(new_seq, top, leaf, qty) for (top, leaf), qty in sorted(usage.items())],
            )
            conn.execute(
                "UPDATE bom_head SET version_seq = ?, updated_at = ? WHERE id = 1",
                (new_seq, _now()),
            )
            self._prune(conn, new_seq)
            created_row = conn.execute(
                "SELECT created_at FROM bom_version WHERE seq = ?", (new_seq,)
            ).fetchone()
            conn.execute("COMMIT")
            return CommitResult(
                new_seq, new_seq, created_row["created_at"], False, 0, len(usage)
            )
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def published_version(self) -> int:
        with self.reader() as conn:
            return int(self._head(conn)["version_seq"])


def _change_obj(item: Mapping[str, Any]) -> MergeChange:
    return MergeChange(
        item["parent"], item["child"], item["base"], item["upload"], item["current"],
        item["decision"], item["result"],
    )


def _merge_counts(
    changes: Sequence[MergeChange],
    conflicts: Sequence[MergeChange],
    final_edges: Sequence[Edge],
) -> dict[str, int]:
    counts = {"changed": 0, "added": 0, "deleted": 0, "merged": 0, "conflict": len(conflicts)}
    for change in changes:
        if change.kind in counts:
            counts[change.kind] += 1
    counts["final_edges"] = len(final_edges)
    counts["final_parts"] = len(set(parts_from_edges(final_edges)))
    return counts


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _checksum(edges: Sequence[Edge]) -> str:
    import hashlib

    payload = "\n".join(f"{e.parent},{e.child},{e.qty}" for e in edges)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
