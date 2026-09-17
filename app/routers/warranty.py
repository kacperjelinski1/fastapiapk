import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.security import CurrentUser, require_owner
from app.database import engine


router = APIRouter(
    prefix="/receptions",
    tags=["warranty"],
)


class WarrantyUpdate(BaseModel):
    is_warranty_repair: bool


class WarrantyLinkUpdate(BaseModel):
    parent_order_id: str


def parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Nieprawidłowe ID przyjęcia."
        )


@router.get("/{reception_id}/warranty")
def get_warranty(
    reception_id: str,
    user: CurrentUser = Depends(require_owner)
):
    reception_uuid = parse_uuid(reception_id)

    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT
                    id,
                    reception_number,
                    is_warranty_repair,
                    warranty_parent_order_id
                FROM service.service_orders
                WHERE id = :id
                """
            ),
            {
                "id": reception_uuid
            }
        ).mappings().first()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Nie znaleziono przyjęcia."
        )

    return {
        "id": str(row["id"]),
        "reception_number": row["reception_number"],
        "is_warranty_repair": bool(row["is_warranty_repair"]),
        "warranty_parent_order_id":
            str(row["warranty_parent_order_id"])
            if row["warranty_parent_order_id"]
            else None
    }


@router.put("/{reception_id}/warranty")
def update_warranty(
    reception_id: str,
    body: WarrantyUpdate,
    user: CurrentUser = Depends(require_owner)
):
    reception_uuid = parse_uuid(reception_id)

    with engine.begin() as connection:

        current = connection.execute(
            text(
                """
                SELECT
                    warranty_parent_order_id
                FROM service.service_orders
                WHERE id = :id
                """
            ),
            {
                "id": reception_uuid
            }
        ).mappings().first()

        if current is None:
            raise HTTPException(
                status_code=404,
                detail="Nie znaleziono przyjęcia."
            )

        if (
            body.is_warranty_repair
            and current["warranty_parent_order_id"] is None
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Naprawa gwarancyjna musi być "
                    "powiązana z wcześniejszym zleceniem."
                )
            )

        row = connection.execute(
            text(
                """
                UPDATE service.service_orders
                SET
                    is_warranty_repair = :is_warranty_repair,
                    warranty_parent_order_id =
                        CASE
                            WHEN :is_warranty_repair
                            THEN warranty_parent_order_id
                            ELSE NULL
                        END,
                    updated_by = :owner_id
                WHERE id = :id
                RETURNING
                    id,
                    reception_number,
                    is_warranty_repair,
                    warranty_parent_order_id
                """
            ),
            {
                "id": reception_uuid,
                "is_warranty_repair":
                    body.is_warranty_repair,
                "owner_id":
                    uuid.UUID(user.id)
            }
        ).mappings().first()

    return {
        "id": str(row["id"]),
        "reception_number": row["reception_number"],
        "is_warranty_repair":
            bool(row["is_warranty_repair"]),
        "warranty_parent_order_id":
            str(row["warranty_parent_order_id"])
            if row["warranty_parent_order_id"]
            else None
    }


@router.put("/{reception_id}/warranty-link")
def set_warranty_link(
    reception_id: str,
    body: WarrantyLinkUpdate,
    user: CurrentUser = Depends(require_owner)
):
    reception_uuid = parse_uuid(reception_id)
    parent_uuid = parse_uuid(body.parent_order_id)

    if reception_uuid == parent_uuid:
        raise HTTPException(
            status_code=400,
            detail=(
                "Zlecenie nie może być "
                "powiązane samo ze sobą."
            )
        )

    with engine.begin() as connection:

        current = connection.execute(
            text(
                """
                SELECT id
                FROM service.service_orders
                WHERE id = :id
                """
            ),
            {
                "id": reception_uuid
            }
        ).first()

        if current is None:
            raise HTTPException(
                status_code=404,
                detail="Nie znaleziono bieżącego zlecenia."
            )

        parent = connection.execute(
            text(
                """
                SELECT
                    id,
                    reception_number,
                    status
                FROM service.service_orders
                WHERE id = :id
                """
            ),
            {
                "id": parent_uuid
            }
        ).mappings().first()

        if parent is None:
            raise HTTPException(
                status_code=404,
                detail="Nie znaleziono wcześniejszego zlecenia."
            )

        if str(parent["status"]) != "COMPLETED":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Naprawę gwarancyjną można powiązać "
                    "tylko z zakończonym zleceniem."
                )
            )

        row = connection.execute(
            text(
                """
                UPDATE service.service_orders
                SET
                    is_warranty_repair = true,
                    warranty_parent_order_id = :parent_id,
                    updated_by = :owner_id
                WHERE id = :id
                RETURNING
                    id,
                    reception_number,
                    is_warranty_repair,
                    warranty_parent_order_id
                """
            ),
            {
                "id": reception_uuid,
                "parent_id": parent_uuid,
                "owner_id": uuid.UUID(user.id)
            }
        ).mappings().first()

    return {
        "id": str(row["id"]),
        "reception_number": row["reception_number"],
        "is_warranty_repair": True,
        "warranty_parent_order_id":
            str(row["warranty_parent_order_id"]),
        "parent_reception_number":
            parent["reception_number"]
    }


@router.delete("/{reception_id}/warranty-link")
def remove_warranty_link(
    reception_id: str,
    user: CurrentUser = Depends(require_owner)
):
    reception_uuid = parse_uuid(reception_id)

    with engine.begin() as connection:

        row = connection.execute(
            text(
                """
                UPDATE service.service_orders
                SET
                    is_warranty_repair = false,
                    warranty_parent_order_id = NULL,
                    updated_by = :owner_id
                WHERE id = :id
                RETURNING
                    id,
                    reception_number
                """
            ),
            {
                "id": reception_uuid,
                "owner_id": uuid.UUID(user.id)
            }
        ).mappings().first()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Nie znaleziono przyjęcia."
        )

    return {
        "id": str(row["id"]),
        "reception_number": row["reception_number"],
        "is_warranty_repair": False,
        "warranty_parent_order_id": None
    }
