import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.database import engine
from app.security import CurrentUser, require_staff


router = APIRouter(
    prefix="/top-secret",
    tags=["top-secret"],
)


class TopSecretUpdate(BaseModel):
    text: str = ""


def _reception_uuid(value: str):
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Nieprawidłowe ID przyjęcia.",
        ) from exc


def _require_owner(user: CurrentUser):
    if user.role != "OWNER":
        raise HTTPException(
            status_code=403,
            detail="TOP SECRET jest dostępne wyłącznie dla właściciela.",
        )


@router.get("/{reception_id}")
def get_top_secret(
    reception_id: str,
    user: CurrentUser = Depends(require_staff),
):
    _require_owner(user)
    reception_uuid = _reception_uuid(reception_id)

    try:
        with engine.connect() as connection:
            reception_exists = connection.execute(
                text(
                    """
                    SELECT 1
                    FROM service.service_orders
                    WHERE id = :id
                    """
                ),
                {"id": reception_uuid},
            ).scalar_one_or_none()

            if reception_exists is None:
                raise HTTPException(
                    status_code=404,
                    detail="Nie znaleziono przyjęcia.",
                )

            row = connection.execute(
                text(
                    """
                    SELECT secret_text, updated_at
                    FROM service.service_order_top_secret
                    WHERE service_order_id = :id
                    """
                ),
                {"id": reception_uuid},
            ).mappings().first()

        if row is None:
            return {
                "reception_id": str(reception_uuid),
                "text": "",
                "updated_at": None,
            }

        return {
            "reception_id": str(reception_uuid),
            "text": row["secret_text"] or "",
            "updated_at": (
                row["updated_at"].isoformat()
                if row["updated_at"]
                else None
            ),
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Błąd odczytu TOP SECRET: {exc}",
        ) from exc


@router.put("/{reception_id}")
def save_top_secret(
    reception_id: str,
    body: TopSecretUpdate,
    user: CurrentUser = Depends(require_staff),
):
    _require_owner(user)
    reception_uuid = _reception_uuid(reception_id)
    owner_uuid = uuid.UUID(user.id)

    try:
        with engine.begin() as connection:
            reception_exists = connection.execute(
                text(
                    """
                    SELECT 1
                    FROM service.service_orders
                    WHERE id = :id
                    """
                ),
                {"id": reception_uuid},
            ).scalar_one_or_none()

            if reception_exists is None:
                raise HTTPException(
                    status_code=404,
                    detail="Nie znaleziono przyjęcia.",
                )

            row = connection.execute(
                text(
                    """
                    INSERT INTO service.service_order_top_secret (
                        service_order_id,
                        secret_text,
                        created_by,
                        updated_by
                    )
                    VALUES (
                        :reception_id,
                        :secret_text,
                        :owner_id,
                        :owner_id
                    )
                    ON CONFLICT (service_order_id)
                    DO UPDATE SET
                        secret_text = EXCLUDED.secret_text,
                        updated_by = EXCLUDED.updated_by
                    RETURNING secret_text, updated_at
                    """
                ),
                {
                    "reception_id": reception_uuid,
                    "secret_text": body.text,
                    "owner_id": owner_uuid,
                },
            ).mappings().one()

        return {
            "reception_id": str(reception_uuid),
            "text": row["secret_text"] or "",
            "updated_at": (
                row["updated_at"].isoformat()
                if row["updated_at"]
                else None
            ),
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Błąd zapisu TOP SECRET: {exc}",
        ) from exc
