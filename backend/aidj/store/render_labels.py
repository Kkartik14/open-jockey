"""Render-label repository and feedback rollups."""

from __future__ import annotations

from aidj.store import db
from aidj.store.analysis_labels import UNTAGGED_GENRE
from aidj.store.models import RenderLabel, RenderLabelKind, RenderTechnique

BAD_RENDER_LABELS: frozenset[RenderLabelKind] = frozenset(
    {
        RenderLabelKind.OFF_BEAT,
        RenderLabelKind.BAD_CUE,
        RenderLabelKind.BAD_ENERGY,
        RenderLabelKind.BAD_KEY,
        RenderLabelKind.CLIPPING,
        RenderLabelKind.WRONG_TEMPO_MATCH,
        RenderLabelKind.TOO_ABRUPT,
        RenderLabelKind.TOO_LONG,
        RenderLabelKind.BORING,
        RenderLabelKind.UNUSABLE,
    }
)


def normalize_family(value: str | None) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else UNTAGGED_GENRE


def pair_family_key(
    *,
    from_beat_source: str,
    to_beat_source: str,
    from_genre: str | None,
    to_genre: str | None,
) -> str:
    return (
        f"{from_beat_source}->{to_beat_source}|"
        f"{normalize_family(from_genre)}->{normalize_family(to_genre)}"
    )


def add(*, render_id: int, kind: RenderLabelKind, notes: str | None = None) -> RenderLabel:
    cur = db.execute(
        "INSERT INTO render_labels(render_id, kind, notes) VALUES (?, ?, ?)",
        (render_id, kind.value, notes),
    )
    label_id = int(cur.lastrowid or 0)
    label = get(label_id)
    if label is None:  # pragma: no cover - INSERT just succeeded
        raise RuntimeError(f"failed to read back render label id={label_id}")
    return label


def get(label_id: int) -> RenderLabel | None:
    row = db.fetch_one("SELECT * FROM render_labels WHERE id=?", (label_id,))
    return RenderLabel.from_row(row) if row else None


def list_for_render(render_id: int) -> list[RenderLabel]:
    rows = db.fetch_all(
        "SELECT * FROM render_labels WHERE render_id=? ORDER BY created_at, id",
        (render_id,),
    )
    return [RenderLabel.from_row(row) for row in rows]


def list_for_renders(render_ids: list[int]) -> dict[int, list[RenderLabel]]:
    if not render_ids:
        return {}
    placeholders = ",".join(["?"] * len(render_ids))
    rows = db.fetch_all(
        f"SELECT * FROM render_labels "
        f"WHERE render_id IN ({placeholders}) "
        f"ORDER BY render_id, created_at, id",
        tuple(render_ids),
    )
    out: dict[int, list[RenderLabel]] = {rid: [] for rid in render_ids}
    for row in rows:
        label = RenderLabel.from_row(row)
        out.setdefault(label.render_id, []).append(label)
    return out


def delete(label_id: int) -> bool:
    cur = db.execute("DELETE FROM render_labels WHERE id=?", (label_id,))
    return cur.rowcount > 0


def counts_for_render(render_id: int) -> dict[RenderLabelKind, int]:
    rows = db.fetch_all(
        "SELECT kind, COUNT(*) AS n FROM render_labels WHERE render_id=? GROUP BY kind",
        (render_id,),
    )
    return {RenderLabelKind(row["kind"]): int(row["n"]) for row in rows}


def counts_as_pass(render_id: int) -> bool:
    counts = counts_for_render(render_id)
    return counts.get(RenderLabelKind.GOOD, 0) > 0 and not any(
        counts.get(kind, 0) > 0 for kind in BAD_RENDER_LABELS
    )


def rollup_by_technique() -> dict[RenderTechnique, dict[RenderLabelKind, int]]:
    rows = db.fetch_all(
        "SELECT ra.technique AS technique, l.kind AS kind, COUNT(*) AS n "
        "FROM render_labels l "
        "JOIN render_artifacts ra ON ra.id = l.render_id "
        "GROUP BY ra.technique, l.kind"
    )
    out: dict[RenderTechnique, dict[RenderLabelKind, int]] = {}
    for row in rows:
        out.setdefault(RenderTechnique(row["technique"]), {})[RenderLabelKind(row["kind"])] = int(
            row["n"]
        )
    return out


def rollup_by_candidate_pair() -> dict[str, dict[RenderLabelKind, int]]:
    rows = db.fetch_all(
        "SELECT ra.from_track AS from_track, ra.to_track AS to_track, "
        "       l.kind AS kind, COUNT(*) AS n "
        "FROM render_labels l "
        "JOIN render_artifacts ra ON ra.id = l.render_id "
        "GROUP BY ra.from_track, ra.to_track, l.kind"
    )
    out: dict[str, dict[RenderLabelKind, int]] = {}
    for row in rows:
        key = f"{row['from_track']}->{row['to_track']}"
        out.setdefault(key, {})[RenderLabelKind(row["kind"])] = int(row["n"])
    return out


def rollup_by_technique_and_pair() -> dict[tuple[RenderTechnique, str], dict[RenderLabelKind, int]]:
    rows = db.fetch_all(
        "SELECT ra.technique AS technique, "
        "       json_extract(ra.request_config_json, '$.confidence_snapshot.from_beat_source') "
        "         AS from_source, "
        "       json_extract(ra.request_config_json, '$.confidence_snapshot.to_beat_source') "
        "         AS to_source, "
        "       ft.genre AS from_genre, "
        "       tt.genre AS to_genre, "
        "       l.kind AS kind, "
        "       COUNT(*) AS n "
        "FROM render_labels l "
        "JOIN render_artifacts ra ON ra.id = l.render_id "
        "JOIN tracks ft ON ft.content_hash = ra.from_track "
        "JOIN tracks tt ON tt.content_hash = ra.to_track "
        "GROUP BY ra.technique, from_source, to_source, ft.genre, tt.genre, l.kind"
    )
    out: dict[tuple[RenderTechnique, str], dict[RenderLabelKind, int]] = {}
    for row in rows:
        family = pair_family_key(
            from_beat_source=row["from_source"] or "unknown",
            to_beat_source=row["to_source"] or "unknown",
            from_genre=row["from_genre"],
            to_genre=row["to_genre"],
        )
        key = (RenderTechnique(row["technique"]), family)
        out.setdefault(key, {})[RenderLabelKind(row["kind"])] = int(row["n"])
    return out
