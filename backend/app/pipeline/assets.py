"""Character, scene, prop, and costume assets.

Inspired by the asset library in Jellyfish (Apache-2.0): a versioned card,
bound to shots, so editing one card can refresh every shot that uses it.
This module is original to 帧川.
"""

from app.db.models import Asset, AssetVersion, Character, Location, Shot, ShotAsset
from app.pipeline.edits import mark_downstream

VIEWS = {
    "character": ["front", "side", "back"],
    "scene": ["front", "left", "right", "back"],
    "prop": ["hero"],
    "costume": ["front"],
}


def _snapshot(db, asset: Asset, note: str) -> None:
    db.add(
        AssetVersion(
            asset_id=asset.id,
            number=int(asset.version or 1),
            name=asset.name,
            description=asset.description,
            prompt=asset.prompt,
            meta=dict(asset.meta or {}),
            note=note,
        )
    )


def ensure_asset(db, project_id: str, kind: str, name: str, description: str = "", prompt: str = "") -> Asset:
    row = (
        db.query(Asset)
        .filter(Asset.project_id == project_id, Asset.kind == kind, Asset.name == name)
        .one_or_none()
    )
    if row is not None:
        return row
    row = Asset(
        project_id=project_id,
        kind=kind,
        name=name,
        description=description,
        prompt=prompt or description,
        version=1,
        status="ready",
        meta={"views": {}},
    )
    db.add(row)
    db.flush()
    _snapshot(db, row, "初始")
    return row


def bind_shot(db, shot: Shot, asset: Asset) -> None:
    existing = (
        db.query(ShotAsset).filter(ShotAsset.shot_id == shot.id, ShotAsset.asset_id == asset.id).one_or_none()
    )
    if existing is None:
        db.add(ShotAsset(shot_id=shot.id, asset_id=asset.id))


def bind_project_cards(db, project_id: str) -> None:
    characters = db.query(Character).filter(Character.project_id == project_id).all()
    locations = db.query(Location).filter(Location.project_id == project_id).all()
    shots = db.query(Shot).filter(Shot.project_id == project_id).all()
    by_name = {character.name: ensure_asset(db, project_id, "character", character.name, character.appearance, character.appearance) for character in characters}
    by_place = {location.name: ensure_asset(db, project_id, "scene", location.name, location.description, location.description) for location in locations}
    for shot in shots:
        for name in shot.character_names or []:
            asset = by_name.get(name)
            if asset is not None:
                bind_shot(db, shot, asset)
    db.flush()


def edit_asset(db, asset: Asset, *, name: str | None = None, description: str | None = None, prompt: str | None = None) -> list[str]:
    _snapshot(db, asset, "编辑前")
    if name:
        asset.name = name
    if description is not None:
        asset.description = description
    if prompt is not None:
        asset.prompt = prompt
    asset.version = int(asset.version or 1) + 1
    asset.status = "edited"
    _snapshot(db, asset, "编辑")
    stale: list[str] = []
    links = db.query(ShotAsset).filter(ShotAsset.asset_id == asset.id).all()
    for link in links:
        shot = db.get(Shot, link.shot_id)
        if shot is not None:
            stale.extend(mark_downstream(db, shot))
    return sorted(set(stale))


def rollback_asset(db, asset: Asset, number: int) -> Asset:
    version = (
        db.query(AssetVersion)
        .filter(AssetVersion.asset_id == asset.id, AssetVersion.number == number)
        .order_by(AssetVersion.created_at.desc())
        .first()
    )
    if version is None:
        raise ValueError("没有这个版本")
    _snapshot(db, asset, f"回滚到 {number} 之前")
    asset.name = version.name
    asset.description = version.description
    asset.prompt = version.prompt
    asset.meta = dict(version.meta or {})
    asset.version = int(asset.version or 1) + 1
    asset.status = "rolled_back"
    return asset


def import_card(db, project_id: str, source: Asset) -> Asset:
    copy = Asset(
        project_id=project_id,
        kind=source.kind,
        name=source.name,
        description=source.description,
        prompt=source.prompt,
        version=1,
        status="ready",
        source_id=source.id,
        meta=dict(source.meta or {}),
    )
    db.add(copy)
    db.flush()
    _snapshot(db, copy, f"来自 {source.id}")
    return copy


def generate_views(db, project, asset: Asset, router, store, save_bytes) -> dict:
    angles = VIEWS.get(asset.kind, ["hero"])
    views = dict((asset.meta or {}).get("views") or {})
    for angle in angles:
        prompt = f"{asset.prompt or asset.description}。视角：{angle}。纯色背景，不要文字。"
        result = router.image(prompt=prompt, width=project.width, height=project.height, seed=1, reference_images=[])
        artifact = save_bytes(
            db,
            store,
            project_id=project.id,
            entity_type="asset",
            entity_id=asset.id,
            kind=f"view_{angle}",
            data=result.data,
            content_type="image/png",
            job_id=None,
            meta={"angle": angle, "provider": result.provider},
        )
        views[angle] = artifact.id
    meta = dict(asset.meta or {})
    meta["views"] = views
    asset.meta = meta
    asset.status = "ready"
    return views
