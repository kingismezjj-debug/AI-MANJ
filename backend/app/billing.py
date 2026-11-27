"""Credit ledger.

A run pre-holds estimate × markup, then settles against the metered model_calls
total. The difference is refunded or charged. Amounts are USD credits, the same
unit as adapters/pricing.py, so a relay can mark them up.
"""

from app.adapters.pricing import cost_of
from app.config import get_settings
from app.db.models import LedgerEntry, ModelCall, User
from app.pipeline.controls import ControlError


def markup() -> float:
    value = float(get_settings().credit_markup or 1)
    return value if value > 0 else 1.0


def metered_usd(db, project_id: str) -> float:
    calls = db.query(ModelCall).filter(ModelCall.project_id == project_id, ModelCall.status == "succeeded").all()
    return round(sum(call.cost_usd or 0 for call in calls), 6)


def _entry(db, user: User, project_id: str | None, kind: str, amount: float, note: str) -> None:
    user.credit_balance = round(float(user.credit_balance or 0) + amount, 6)
    db.add(
        LedgerEntry(
            user_id=user.id,
            project_id=project_id,
            kind=kind,
            amount=round(amount, 6),
            balance_after=user.credit_balance,
            note=note,
        )
    )


def hold_for_run(db, user: User, project, estimate_usd: float) -> float:
    amount = round(max(0.0, float(estimate_usd)) * markup(), 6)
    if float(user.credit_balance or 0) + 1e-9 < amount:
        raise ControlError(
            f"积分不足，本次预扣 {amount:.3f}，余额 {float(user.credit_balance or 0):.3f}",
            402,
        )
    if amount:
        _entry(db, user, project.id, "hold", -amount, "开拍预扣")
    settings = dict(project.settings or {})
    settings["credit_hold"] = amount
    settings["credit_markup"] = markup()
    settings["settled"] = False
    project.settings = settings
    return amount


def settle_project(db, project) -> dict:
    settings = dict(project.settings or {})
    if settings.get("settled"):
        return {"settled": True, "charge": settings.get("credit_charge", 0)}
    hold = float(settings.get("credit_hold") or 0)
    rate = float(settings.get("credit_markup") or markup())
    charge = round(metered_usd(db, project.id) * rate, 6)
    user = db.get(User, project.user_id) if project.user_id else None
    if user is not None and (hold or charge):
        if hold:
            _entry(db, user, project.id, "release", hold, "退回预扣")
        if charge:
            _entry(db, user, project.id, "settle", -charge, "按实际计量结算")
    settings["settled"] = True
    settings["credit_charge"] = charge
    settings["credit_hold"] = 0
    project.settings = settings
    return {"settled": True, "charge": charge, "released": hold}


def top_up(db, user: User, amount: float, note: str) -> User:
    if amount <= 0:
        raise ControlError("充值金额要大于 0", 400)
    _entry(db, user, None, "topup", round(amount, 6), note or "管理员充值")
    db.commit()
    db.refresh(user)
    return user
