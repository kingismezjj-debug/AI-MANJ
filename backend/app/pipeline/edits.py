"""Line diff for a rewrite, and the shots a script edit puts out of date."""

import difflib

from app.db.models import Episode, Scene, Shot


def hunks(before: str, after: str) -> list[dict]:
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    rows = []
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        rows.append(
            {
                "tag": tag,
                "before": "\n".join(before_lines[i1:i2]),
                "after": "\n".join(after_lines[j1:j2]),
            }
        )
    return rows


def apply_hunks(before: str, proposed: str, accepted: list[int]) -> str:
    """Keep accepted hunks from the proposal and the rest from the original."""
    pieces = hunks(before, proposed)
    if not pieces:
        return before
    chosen = set(accepted)
    # Rebuild by walking the proposal only where a hunk was accepted.
    # For a single replacement this is the whole proposal or the original.
    if set(range(len(pieces))) == chosen:
        return proposed
    if not chosen:
        return before
    kept = list((before or "").splitlines())
    # Apply accepted replacements from the end so indexes stay valid.
    matcher = difflib.SequenceMatcher(a=(before or "").splitlines(), b=(proposed or "").splitlines())
    opcodes = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    for index in sorted(chosen, reverse=True):
        if index >= len(opcodes):
            continue
        tag, i1, i2, j1, j2 = opcodes[index]
        proposed_lines = (proposed or "").splitlines()
        if tag == "insert":
            kept[i1:i1] = proposed_lines[j1:j2]
        else:
            kept[i1:i2] = proposed_lines[j1:j2]
    return "\n".join(kept)


def mark_downstream(db, shot: Shot) -> list[str]:
    """This shot and later shots in the same episode need a new picture."""
    later = (
        db.query(Shot)
        .filter(Shot.episode_id == shot.episode_id, Shot.number >= shot.number)
        .all()
    )
    ids = []
    for item in later:
        item.stale = True
        status = dict(item.stage_status or {})
        status["storyboard"] = "stale"
        status["video"] = "stale"
        item.stage_status = status
        ids.append(item.id)
    episode = db.get(Episode, shot.episode_id)
    if episode is not None and episode.status == "ready":
        episode.status = "stale"
    return ids


def affected_report(db, shot: Shot) -> dict:
    episode = db.get(Episode, shot.episode_id)
    scene = db.get(Scene, shot.scene_id)
    later = (
        db.query(Shot)
        .filter(Shot.episode_id == shot.episode_id, Shot.number >= shot.number)
        .order_by(Shot.number)
        .all()
    )
    return {
        "episode_id": shot.episode_id,
        "episode_number": episode.number if episode else None,
        "scene_id": shot.scene_id,
        "location": scene.location_name if scene else "",
        "shot_ids": [item.id for item in later],
    }
