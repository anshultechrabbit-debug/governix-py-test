"""Security alerts: when someone's two-step sign-in changes, they and their managers hear of it at once.

A thief who has a password can sign in to an account that has no authenticator yet and set up their own phone; or
someone may use a stolen backup code. The person may never see an in-app message their thief can read and dismiss,
so their managers (the people who can deactivate the account or reset it) are told too.
"""

import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.modules.auth.permissions import Role
from app.modules.notifications.model import NotificationType
from app.modules.notifications.service import notify
from app.modules.users.model import User

IF_NOT_YOU = "If this wasn't you, tell your admin right away: someone may know your password."
IF_NOT_THEM = ("If they didn't do this, deactivate the account or reset its password and two-step sign-in: "
               "someone may know their password.")


def account_managers(session: Session, user: User) -> list[uuid.UUID]:
    """Who can act on this account: its Branch Manager and Org Admins, for an Org Admin also the Master Admins."""
    query = select(User.id).where(User.is_active, User.deleted_at.is_(None), User.id != user.id)
    role = Role(user.role)
    if role is Role.DEPARTMENT_USER:
        query = query.where(User.organization_id == user.organization_id, or_(
            and_(User.role == Role.BRANCH_MANAGER, User.branch_id == user.branch_id), User.role == Role.ORG_ADMIN))
    elif role is Role.BRANCH_MANAGER:
        query = query.where(User.organization_id == user.organization_id, User.role == Role.ORG_ADMIN)
    elif role is Role.ORG_ADMIN:
        query = query.where(or_(and_(User.organization_id == user.organization_id, User.role == Role.ORG_ADMIN),
                                User.role == Role.MASTER_ADMIN))
    else:  # Master Admin, Support Agent
        query = query.where(User.role == Role.MASTER_ADMIN)
    return list(session.scalars(query))


def two_step_changed(session: Session, user: User, event: str, *, actor: User | None = None,
                     backup_codes_left: int | None = None) -> None:
    """event: "enabled" | "disabled" | "reset" | "recovery_used" | "recovery_renewed"."""
    name = user.full_name
    by = actor.full_name if actor is not None else "a Governix server administrator"
    to_user = {
        "enabled": ("Two-step sign-in was turned on for your account", IF_NOT_YOU),
        "disabled": ("Two-step sign-in was turned off for your account", IF_NOT_YOU),
        "reset": (f"Your two-step sign-in was reset by {by}",
                  "Sign in with your password and set it up again from the account menu."),
        "recovery_used": ("A backup code was used to sign in to your account",
                          f"{backup_codes_left} backup codes are left. {IF_NOT_YOU}"),
        "recovery_renewed": ("New backup codes were made for your account", "The old ones no longer work. " + IF_NOT_YOU),
    }[event]
    to_managers = {
        "enabled": (f"{name} turned on two-step sign-in", IF_NOT_THEM),
        "disabled": (f"{name} turned off two-step sign-in", IF_NOT_THEM),
        "reset": (f"{by} reset the two-step sign-in of {name}", "They can now sign in with only their password until they "
                  "set it up again. Make sure it was really them who asked."),
        "recovery_used": (f"{name} signed in with a backup code", f"{backup_codes_left} backup codes are left. {IF_NOT_THEM}"),
    }.get(event)
    data = {"event": event, "user_id": str(user.id)}
    notify(session, [user.id], NotificationType.SECURITY_ALERT, to_user[0], body=to_user[1],
           organization_id=user.organization_id, data=data)
    if to_managers is not None:
        notify(session, account_managers(session, user), NotificationType.SECURITY_ALERT, to_managers[0],
               body=to_managers[1], organization_id=user.organization_id, link="/users", data=data,
               exclude=actor.id if actor is not None else None)
