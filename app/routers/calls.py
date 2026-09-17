import hashlib
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.config import settings
from app.database import engine
from app.security import CurrentUser, require_owner


router = APIRouter(
    prefix="/calls",
    tags=["calls"],
)


def _uuid(
    value: str,
    label: str = "ID",
):
    try:
        return uuid.UUID(value)

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Nieprawidłowe {label}.",
        ) from exc


def _validate_direction(
    direction: str,
) -> str:

    value = direction.upper().strip()

    if value not in {
        "INCOMING",
        "OUTGOING",
        "UNKNOWN",
    }:
        raise HTTPException(
            status_code=400,
            detail="Nieprawidłowy kierunek rozmowy.",
        )

    return value


def _upsert_phone(
    connection,
    phone: str,
):

    return connection.execute(
        text(
            """
            INSERT INTO core.phone_numbers (
                e164,
                display_number,
                first_seen_at,
                last_seen_at,
                source
            )
            VALUES (
                core.normalize_phone(:phone),
                :phone,
                now(),
                now(),
                'ANDROID_CALL'
            )
            ON CONFLICT (e164)
            DO UPDATE SET
                display_number = EXCLUDED.display_number,
                last_seen_at = now()
            RETURNING
                id,
                match_key,
                e164,
                display_number
            """
        ),
        {
            "phone": phone,
        },
    ).mappings().one()


def _client_for_match_key(
    connection,
    match_key: str,
):

    return connection.execute(
        text(
            """
            SELECT c.id
            FROM core.clients c
            JOIN core.client_phones cp
                ON cp.client_id = c.id
            JOIN core.phone_numbers p
                ON p.id = cp.phone_number_id
            WHERE p.match_key = :match_key
              AND c.is_active = TRUE
            ORDER BY c.created_at ASC
            LIMIT 1
            """
        ),
        {
            "match_key": match_key,
        },
    ).scalar_one_or_none()


def _active_orders_for_match_key(
    connection,
    match_key: str,
):

    return connection.execute(
        text(
            """
            SELECT
                so.id,
                so.reception_number,
                so.status
            FROM service.service_orders so
            LEFT JOIN core.phone_numbers p1
                ON p1.id = so.primary_phone_id
            LEFT JOIN core.phone_numbers p2
                ON p2.id = so.secondary_phone_id
            WHERE (
                    p1.match_key = :match_key
                    OR
                    p2.match_key = :match_key
                  )
              AND so.status IN (
                    'IN_SERVICE',
                    'READY_FOR_PICKUP'
                  )
            ORDER BY so.received_at DESC
            """
        ),
        {
            "match_key": match_key,
        },
    ).mappings().all()


def _existing_recording_by_hash(
    connection,
    sha256: str,
    size_bytes: int,
):

    return connection.execute(
        text(
            """
            SELECT
                cr.id AS recording_id,
                cr.call_id,
                so.id AS storage_object_id,
                so.object_key,
                c.started_at,
                c.direction,
                c.duration_seconds,
                p.display_number,
                p.e164
            FROM core.storage_objects so
            JOIN core.call_recordings cr
                ON cr.storage_object_id = so.id
            JOIN core.calls c
                ON c.id = cr.call_id
            JOIN core.phone_numbers p
                ON p.id = c.phone_number_id
            WHERE so.storage_area = 'calls'
              AND so.sha256 = :sha256
              AND so.size_bytes = :size_bytes
              AND so.deleted_at IS NULL
            LIMIT 1
            """
        ),
        {
            "sha256": sha256,
            "size_bytes": size_bytes,
        },
    ).mappings().first()


def _safe_recording_file(
    object_key: str,
):

    root = Path(
        settings.media_root
    ).resolve()

    path = (
        root /
        object_key
    ).resolve()

    try:
        path.relative_to(
            root
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail="Nieprawidłowa ścieżka nagrania.",
        ) from exc

    return path


def _write_upload_to_path(
    upload_file: UploadFile,
    path: Path,
):

    sha256 = hashlib.sha256()
    size_bytes = 0

    with path.open(
        "wb"
    ) as output_file:

        while True:

            chunk = upload_file.file.read(
                1024 * 1024
            )

            if not chunk:
                break

            output_file.write(
                chunk
            )

            sha256.update(
                chunk
            )

            size_bytes += len(
                chunk
            )

    if size_bytes == 0:
        raise HTTPException(
            status_code=400,
            detail="Przesłany plik jest pusty.",
        )

    return (
        sha256.hexdigest(),
        size_bytes,
    )


class CallCreate(BaseModel):

    phone_number: str = Field(
        min_length=1
    )

    direction: str = "UNKNOWN"

    started_at: datetime

    duration_seconds: int | None = Field(
        default=None,
        ge=0,
    )

    source: str = "ANDROID_DIALER"


class UploadRuleUpdate(BaseModel):

    rule: str


@router.get("")
def list_calls(
    reception_id: str | None = None,
    client_id: str | None = None,
    phone: str | None = None,
    limit: int = Query(
        100,
        ge=1,
        le=500,
    ),
    user: CurrentUser = Depends(
        require_owner
    ),
):

    conditions = []
    params = {
        "limit": limit,
    }

    if reception_id:

        params["reception_id"] = _uuid(
            reception_id,
            "ID przyjęcia",
        )

        conditions.append(
            """
            EXISTS (
                SELECT 1
                FROM service.service_orders so
                WHERE so.id = :reception_id
                  AND c.phone_number_id IN (
                        so.primary_phone_id,
                        so.secondary_phone_id
                      )
            )
            """
        )

    if client_id:

        params["client_id"] = _uuid(
            client_id,
            "ID klienta",
        )

        conditions.append(
            """
            EXISTS (
                SELECT 1
                FROM core.client_phones cp
                WHERE cp.client_id = :client_id
                  AND cp.phone_number_id = c.phone_number_id
            )
            """
        )

    if phone:

        params["phone"] = phone

        conditions.append(
            """
            p.match_key =
                core.phone_match_key(:phone)
            """
        )

    where_sql = (
        " WHERE " +
        " AND ".join(
            conditions
        )
        if conditions
        else ""
    )

    query = f"""
        SELECT
            c.id,
            c.direction,
            c.started_at,
            c.duration_seconds,
            c.source,
            c.client_id,
            c.service_order_id,
            p.display_number,
            p.e164,
            (
                SELECT COUNT(*)
                FROM core.call_recordings cr
                WHERE cr.call_id = c.id
            ) AS recording_count
        FROM core.calls c
        JOIN core.phone_numbers p
            ON p.id = c.phone_number_id
        {where_sql}
        ORDER BY c.started_at DESC
        LIMIT :limit
    """

    with engine.connect() as connection:

        rows = connection.execute(
            text(
                query
            ),
            params,
        ).mappings().all()

    return [
        {
            "id":
                str(
                    row["id"]
                ),

            "direction":
                str(
                    row["direction"]
                ),

            "started_at":
                row["started_at"]
                .isoformat(),

            "duration_seconds":
                row["duration_seconds"],

            "source":
                row["source"],

            "client_id":
                (
                    str(
                        row["client_id"]
                    )
                    if row["client_id"]
                    else None
                ),

            "legacy_reception_id":
                (
                    str(
                        row["service_order_id"]
                    )
                    if row["service_order_id"]
                    else None
                ),

            "phone_number":
                row["display_number"]
                or row["e164"],

            "recording_count":
                int(
                    row["recording_count"]
                    or 0
                ),
        }
        for row in rows
    ]


@router.get(
    "/match/{phone}"
)
def match_phone(
    phone: str,
    user: CurrentUser = Depends(
        require_owner
    ),
):

    with engine.connect() as connection:

        match_key = connection.execute(
            text(
                """
                SELECT core.phone_match_key(:phone)
                """
            ),
            {
                "phone": phone,
            },
        ).scalar_one()

        client_id = _client_for_match_key(
            connection,
            match_key,
        )

        orders = _active_orders_for_match_key(
            connection,
            match_key,
        )

    return {
        "client_id":
            (
                str(
                    client_id
                )
                if client_id
                else None
            ),

        "known_client":
            client_id is not None,

        "active_receptions":
            [
                {
                    "id":
                        str(
                            order["id"]
                        ),

                    "reception_number":
                        order[
                            "reception_number"
                        ],

                    "status":
                        str(
                            order["status"]
                        ),
                }
                for order in orders
            ],
    }


@router.post("")
def create_call(
    body: CallCreate,
    user: CurrentUser = Depends(
        require_owner
    ),
):

    direction = _validate_direction(
        body.direction
    )

    with engine.begin() as connection:

        phone = _upsert_phone(
            connection,
            body.phone_number,
        )

        client_id = _client_for_match_key(
            connection,
            phone["match_key"],
        )

        call_id = connection.execute(
            text(
                """
                INSERT INTO core.calls (
                    phone_number_id,
                    client_id,
                    service_order_id,
                    direction,
                    started_at,
                    duration_seconds,
                    source
                )
                VALUES (
                    :phone_number_id,
                    :client_id,
                    NULL,
                    CAST(
                        :direction
                        AS core.call_direction
                    ),
                    :started_at,
                    :duration_seconds,
                    :source
                )
                RETURNING id
                """
            ),
            {
                "phone_number_id":
                    phone["id"],

                "client_id":
                    client_id,

                "direction":
                    direction,

                "started_at":
                    body.started_at,

                "duration_seconds":
                    body.duration_seconds,

                "source":
                    body.source,
            },
        ).scalar_one()

    return {
        "id":
            str(
                call_id
            ),

        "client_id":
            (
                str(
                    client_id
                )
                if client_id
                else None
            ),

        "phone_number":
            body.phone_number,

        "reception_id":
            None,
    }


@router.post(
    "/{call_id}/recording"
)
def upload_recording(
    call_id: str,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(
        require_owner
    ),
):

    call_uuid = _uuid(
        call_id,
        "ID rozmowy",
    )

    root = Path(
        settings.media_root
    ).resolve()

    temp_folder = (
        root /
        "calls" /
        "_incoming"
    )

    temp_folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    original_filename = (
        file.filename
        or "recording.mp3"
    )

    suffix = (
        Path(
            original_filename
        ).suffix.lower()
        or ".mp3"
    )

    temp_path = (
        temp_folder /
        f"{uuid.uuid4()}{suffix}"
    )

    try:

        sha256, size_bytes = (
            _write_upload_to_path(
                file,
                temp_path,
            )
        )

        with engine.begin() as connection:

            call = connection.execute(
                text(
                    """
                    SELECT
                        c.id,
                        c.started_at,
                        p.e164
                    FROM core.calls c
                    JOIN core.phone_numbers p
                        ON p.id =
                            c.phone_number_id
                    WHERE c.id = :call_id
                    """
                ),
                {
                    "call_id":
                        call_uuid,
                },
            ).mappings().first()

            if call is None:

                raise HTTPException(
                    status_code=404,
                    detail="Nie znaleziono rozmowy.",
                )

            existing = (
                _existing_recording_by_hash(
                    connection,
                    sha256,
                    size_bytes,
                )
            )

            if existing is not None:

                temp_path.unlink(
                    missing_ok=True
                )

                return {
                    "id":
                        str(
                            existing[
                                "recording_id"
                            ]
                        ),

                    "call_id":
                        str(
                            existing[
                                "call_id"
                            ]
                        ),

                    "duplicate":
                        True,

                    "size_bytes":
                        size_bytes,

                    "sha256":
                        sha256,

                    "content_url":
                        (
                            "/calls/recordings/"
                            f"{existing['recording_id']}"
                            "/content"
                        ),
                }

            final_folder = (
                root /
                "calls" /
                str(
                    call_uuid
                )
            )

            final_folder.mkdir(
                parents=True,
                exist_ok=True,
            )

            final_path = (
                final_folder /
                f"{uuid.uuid4()}{suffix}"
            )

            shutil.move(
                str(
                    temp_path
                ),
                str(
                    final_path
                ),
            )

            object_key = str(
                final_path.relative_to(
                    root
                )
            ).replace(
                "\\",
                "/",
            )

            storage_object_id = (
                connection.execute(
                    text(
                        """
                        INSERT INTO core.storage_objects (
                            storage_area,
                            object_key,
                            original_filename,
                            mime_type,
                            extension,
                            size_bytes,
                            sha256,
                            captured_at,
                            uploaded_at,
                            upload_completed,
                            created_by
                        )
                        VALUES (
                            'calls',
                            :object_key,
                            :original_filename,
                            :mime_type,
                            :extension,
                            :size_bytes,
                            :sha256,
                            :captured_at,
                            now(),
                            TRUE,
                            :created_by
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "object_key":
                            object_key,

                        "original_filename":
                            original_filename,

                        "mime_type":
                            (
                                file.content_type
                                or "audio/mpeg"
                            ),

                        "extension":
                            suffix,

                        "size_bytes":
                            size_bytes,

                        "sha256":
                            sha256,

                        "captured_at":
                            call[
                                "started_at"
                            ],

                        "created_by":
                            _uuid(
                                user.id,
                                "ID użytkownika",
                            ),
                    },
                ).scalar_one()
            )

            recording_id = (
                connection.execute(
                    text(
                        """
                        INSERT INTO core.call_recordings (
                            call_id,
                            storage_object_id,
                            original_filename,
                            parsed_phone_e164,
                            parsed_started_at,
                            import_source
                        )
                        VALUES (
                            :call_id,
                            :storage_object_id,
                            :original_filename,
                            :phone,
                            :started_at,
                            'ANDROID'
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "call_id":
                            call_uuid,

                        "storage_object_id":
                            storage_object_id,

                        "original_filename":
                            original_filename,

                        "phone":
                            call["e164"],

                        "started_at":
                            call[
                                "started_at"
                            ],
                    },
                ).scalar_one()
            )

        return {
            "id":
                str(
                    recording_id
                ),

            "call_id":
                str(
                    call_uuid
                ),

            "duplicate":
                False,

            "size_bytes":
                size_bytes,

            "sha256":
                sha256,

            "content_url":
                (
                    "/calls/recordings/"
                    f"{recording_id}"
                    "/content"
                ),
        }

    except HTTPException:

        temp_path.unlink(
            missing_ok=True
        )

        raise

    except Exception:

        temp_path.unlink(
            missing_ok=True
        )

        raise

    finally:

        file.file.close()


@router.get(
    "/recordings/{recording_id}/content"
)
def recording_content(
    recording_id: str,
    user: CurrentUser = Depends(
        require_owner
    ),
):

    recording_uuid = _uuid(
        recording_id,
        "ID nagrania",
    )

    with engine.connect() as connection:

        row = connection.execute(
            text(
                """
                SELECT
                    so.object_key,
                    so.original_filename,
                    so.mime_type
                FROM core.call_recordings cr
                JOIN core.storage_objects so
                    ON so.id =
                        cr.storage_object_id
                WHERE cr.id =
                    :recording_id
                  AND so.deleted_at IS NULL
                """
            ),
            {
                "recording_id":
                    recording_uuid,
            },
        ).mappings().first()

    if row is None:

        raise HTTPException(
            status_code=404,
            detail="Nie znaleziono nagrania.",
        )

    path = _safe_recording_file(
        row["object_key"]
    )

    if not path.is_file():

        raise HTTPException(
            status_code=404,
            detail="Brak pliku na dysku.",
        )

    return FileResponse(
        path=path,
        media_type=(
            row["mime_type"]
            or "audio/mpeg"
        ),
        filename=(
            row["original_filename"]
            or path.name
        ),
    )


@router.get(
    "/recordings"
)
def list_recordings(
    reception_id: str | None = None,
    client_id: str | None = None,
    phone: str | None = None,
    limit: int = Query(
        200,
        ge=1,
        le=1000,
    ),
    user: CurrentUser = Depends(
        require_owner
    ),
):

    conditions = []
    params = {
        "limit":
            limit,
    }

    if reception_id:

        params[
            "reception_id"
        ] = _uuid(
            reception_id,
            "ID przyjęcia",
        )

        conditions.append(
            """
            EXISTS (
                SELECT 1
                FROM service.service_orders so2
                WHERE so2.id =
                    :reception_id
                  AND c.phone_number_id IN (
                        so2.primary_phone_id,
                        so2.secondary_phone_id
                      )
            )
            """
        )

    if client_id:

        params[
            "client_id"
        ] = _uuid(
            client_id,
            "ID klienta",
        )

        conditions.append(
            """
            EXISTS (
                SELECT 1
                FROM core.client_phones cp
                WHERE cp.client_id =
                    :client_id
                  AND cp.phone_number_id =
                    c.phone_number_id
            )
            """
        )

    if phone:

        params[
            "phone"
        ] = phone

        conditions.append(
            """
            p.match_key =
                core.phone_match_key(:phone)
            """
        )

    where_sql = (
        " WHERE " +
        " AND ".join(
            conditions
        )
        if conditions
        else ""
    )

    query = f"""
        SELECT
            cr.id,
            cr.original_filename,
            cr.imported_at,
            c.id AS call_id,
            c.started_at,
            c.direction,
            c.duration_seconds,
            c.client_id,
            c.service_order_id,
            p.display_number,
            p.e164,
            so.size_bytes,
            so.mime_type,
            so.sha256
        FROM core.call_recordings cr
        JOIN core.calls c
            ON c.id = cr.call_id
        JOIN core.phone_numbers p
            ON p.id =
                c.phone_number_id
        JOIN core.storage_objects so
            ON so.id =
                cr.storage_object_id
        {where_sql}
        AND so.deleted_at IS NULL
        ORDER BY c.started_at DESC
        LIMIT :limit
    """

    if not conditions:

        query = query.replace(
            f"{where_sql}\n        AND so.deleted_at IS NULL",
            """
        WHERE so.deleted_at IS NULL
            """,
        )

    with engine.connect() as connection:

        rows = connection.execute(
            text(
                query
            ),
            params,
        ).mappings().all()

    return [
        {
            "id":
                str(
                    row["id"]
                ),

            "call_id":
                str(
                    row["call_id"]
                ),

            "original_filename":
                row[
                    "original_filename"
                ],

            "started_at":
                row["started_at"]
                .isoformat(),

            "duration_seconds":
                row[
                    "duration_seconds"
                ],

            "direction":
                str(
                    row["direction"]
                ),

            "client_id":
                (
                    str(
                        row["client_id"]
                    )
                    if row[
                        "client_id"
                    ]
                    else None
                ),

            "legacy_reception_id":
                (
                    str(
                        row[
                            "service_order_id"
                        ]
                    )
                    if row[
                        "service_order_id"
                    ]
                    else None
                ),

            "phone_number":
                (
                    row[
                        "display_number"
                    ]
                    or row["e164"]
                    or ""
                ),

            "size_bytes":
                int(
                    row[
                        "size_bytes"
                    ]
                    or 0
                ),

            "mime_type":
                (
                    row["mime_type"]
                    or "audio/mpeg"
                ),

            "sha256":
                (
                    row["sha256"]
                    or ""
                ),

            "content_url":
                (
                    "/calls/recordings/"
                    f"{row['id']}"
                    "/content"
                ),
        }
        for row in rows
    ]


@router.post(
    "/import-recording"
)
def import_recording(
    phone_number: str = Form(...),
    started_at: datetime = Form(...),
    direction: str = Form(
        "UNKNOWN"
    ),
    duration_seconds: int | None = Form(
        None
    ),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(
        require_owner
    ),
):

    normalized_direction = (
        _validate_direction(
            direction
        )
    )

    if (
        duration_seconds is not None
        and duration_seconds < 0
    ):
        raise HTTPException(
            status_code=400,
            detail="Czas rozmowy nie może być ujemny.",
        )

    root = Path(
        settings.media_root
    ).resolve()

    temp_folder = (
        root /
        "calls" /
        "_incoming"
    )

    temp_folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    original_filename = (
        file.filename
        or "recording.mp3"
    )

    suffix = (
        Path(
            original_filename
        ).suffix.lower()
        or ".mp3"
    )

    temp_path = (
        temp_folder /
        f"{uuid.uuid4()}{suffix}"
    )

    final_path = None

    try:

        sha256, size_bytes = (
            _write_upload_to_path(
                file,
                temp_path,
            )
        )

        with engine.begin() as connection:

            existing = (
                _existing_recording_by_hash(
                    connection,
                    sha256,
                    size_bytes,
                )
            )

            if existing is not None:

                temp_path.unlink(
                    missing_ok=True
                )

                return {
                    "duplicate":
                        True,

                    "call":
                        {
                            "id":
                                str(
                                    existing[
                                        "call_id"
                                    ]
                                ),

                            "phone_number":
                                (
                                    existing[
                                        "display_number"
                                    ]
                                    or existing[
                                        "e164"
                                    ]
                                    or ""
                                ),

                            "started_at":
                                existing[
                                    "started_at"
                                ].isoformat(),

                            "direction":
                                str(
                                    existing[
                                        "direction"
                                    ]
                                ),

                            "duration_seconds":
                                existing[
                                    "duration_seconds"
                                ],
                        },

                    "recording":
                        {
                            "id":
                                str(
                                    existing[
                                        "recording_id"
                                    ]
                                ),

                            "size_bytes":
                                size_bytes,

                            "sha256":
                                sha256,

                            "content_url":
                                (
                                    "/calls/recordings/"
                                    f"{existing['recording_id']}"
                                    "/content"
                                ),
                        },
                }

            phone = _upsert_phone(
                connection,
                phone_number,
            )

            client_id = (
                _client_for_match_key(
                    connection,
                    phone[
                        "match_key"
                    ],
                )
            )

            call_id = (
                connection.execute(
                    text(
                        """
                        INSERT INTO core.calls (
                            phone_number_id,
                            client_id,
                            service_order_id,
                            direction,
                            started_at,
                            duration_seconds,
                            source
                        )
                        VALUES (
                            :phone_number_id,
                            :client_id,
                            NULL,
                            CAST(
                                :direction
                                AS core.call_direction
                            ),
                            :started_at,
                            :duration_seconds,
                            'PHONE_IMPORT'
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "phone_number_id":
                            phone["id"],

                        "client_id":
                            client_id,

                        "direction":
                            normalized_direction,

                        "started_at":
                            started_at,

                        "duration_seconds":
                            duration_seconds,
                    },
                ).scalar_one()
            )

            final_folder = (
                root /
                "calls" /
                str(
                    call_id
                )
            )

            final_folder.mkdir(
                parents=True,
                exist_ok=True,
            )

            final_path = (
                final_folder /
                f"{uuid.uuid4()}{suffix}"
            )

            shutil.move(
                str(
                    temp_path
                ),
                str(
                    final_path
                ),
            )

            object_key = str(
                final_path.relative_to(
                    root
                )
            ).replace(
                "\\",
                "/",
            )

            storage_object_id = (
                connection.execute(
                    text(
                        """
                        INSERT INTO core.storage_objects (
                            storage_area,
                            object_key,
                            original_filename,
                            mime_type,
                            extension,
                            size_bytes,
                            sha256,
                            captured_at,
                            uploaded_at,
                            upload_completed,
                            created_by
                        )
                        VALUES (
                            'calls',
                            :object_key,
                            :original_filename,
                            :mime_type,
                            :extension,
                            :size_bytes,
                            :sha256,
                            :captured_at,
                            now(),
                            TRUE,
                            :created_by
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "object_key":
                            object_key,

                        "original_filename":
                            original_filename,

                        "mime_type":
                            (
                                file.content_type
                                or "audio/mpeg"
                            ),

                        "extension":
                            suffix,

                        "size_bytes":
                            size_bytes,

                        "sha256":
                            sha256,

                        "captured_at":
                            started_at,

                        "created_by":
                            _uuid(
                                user.id,
                                "ID użytkownika",
                            ),
                    },
                ).scalar_one()
            )

            recording_id = (
                connection.execute(
                    text(
                        """
                        INSERT INTO core.call_recordings (
                            call_id,
                            storage_object_id,
                            original_filename,
                            parsed_phone_e164,
                            parsed_started_at,
                            import_source
                        )
                        VALUES (
                            :call_id,
                            :storage_object_id,
                            :original_filename,
                            :phone,
                            :started_at,
                            'ANDROID'
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "call_id":
                            call_id,

                        "storage_object_id":
                            storage_object_id,

                        "original_filename":
                            original_filename,

                        "phone":
                            phone["e164"],

                        "started_at":
                            started_at,
                    },
                ).scalar_one()
            )

        return {
            "duplicate":
                False,

            "call":
                {
                    "id":
                        str(
                            call_id
                        ),

                    "client_id":
                        (
                            str(
                                client_id
                            )
                            if client_id
                            else None
                        ),

                    "reception_id":
                        None,

                    "phone_number":
                        phone_number,

                    "started_at":
                        started_at.isoformat(),

                    "direction":
                        normalized_direction,

                    "duration_seconds":
                        duration_seconds,
                },

            "recording":
                {
                    "id":
                        str(
                            recording_id
                        ),

                    "size_bytes":
                        size_bytes,

                    "sha256":
                        sha256,

                    "content_url":
                        (
                            "/calls/recordings/"
                            f"{recording_id}"
                            "/content"
                        ),
                },
        }

    except HTTPException:

        temp_path.unlink(
            missing_ok=True
        )

        if (
            final_path is not None
            and final_path.exists()
        ):
            final_path.unlink(
                missing_ok=True
            )

        raise

    except Exception:

        temp_path.unlink(
            missing_ok=True
        )

        if (
            final_path is not None
            and final_path.exists()
        ):
            final_path.unlink(
                missing_ok=True
            )

        raise

    finally:

        file.file.close()


@router.get(
    "/upload-policy/{phone}"
)
def get_upload_policy(
    phone: str,
    user: CurrentUser = Depends(
        require_owner
    ),
):

    with engine.connect() as connection:

        match_key = connection.execute(
            text(
                """
                SELECT core.phone_match_key(:phone)
                """
            ),
            {
                "phone":
                    phone,
            },
        ).scalar_one()

        row = connection.execute(
            text(
                """
                SELECT
                    p.id,
                    p.display_number,
                    p.e164,
                    cur.rule
                FROM core.phone_numbers p
                LEFT JOIN core.call_upload_rules cur
                    ON cur.phone_number_id =
                        p.id
                WHERE p.match_key =
                    :match_key
                ORDER BY p.created_at ASC
                LIMIT 1
                """
            ),
            {
                "match_key":
                    match_key,
            },
        ).mappings().first()

        client_id = (
            _client_for_match_key(
                connection,
                match_key,
            )
        )

    rule = (
        row["rule"]
        if row
        else None
    )

    if rule == "NEVER":

        recommended_upload = False
        reason = "NEVER"

    elif rule == "ALWAYS":

        recommended_upload = True
        reason = "ALWAYS"

    elif client_id is not None:

        recommended_upload = True
        reason = "KNOWN_CLIENT"

    else:

        recommended_upload = None
        reason = "CONTACT_CHECK_REQUIRED"

    return {
        "phone_number":
            phone,

        "known_client":
            client_id is not None,

        "client_id":
            (
                str(
                    client_id
                )
                if client_id
                else None
            ),

        "rule":
            rule,

        "recommended_upload":
            recommended_upload,

        "reason":
            reason,
    }


@router.get(
    "/upload-rules"
)
def list_upload_rules(
    user: CurrentUser = Depends(
        require_owner
    ),
):

    with engine.connect() as connection:

        rows = connection.execute(
            text(
                """
                SELECT
                    cur.id,
                    cur.rule,
                    cur.created_at,
                    cur.updated_at,
                    p.id AS phone_number_id,
                    p.display_number,
                    p.e164
                FROM core.call_upload_rules cur
                JOIN core.phone_numbers p
                    ON p.id =
                        cur.phone_number_id
                ORDER BY
                    cur.rule,
                    p.display_number,
                    p.e164
                """
            )
        ).mappings().all()

    return [
        {
            "id":
                str(
                    row["id"]
                ),

            "phone_number_id":
                str(
                    row[
                        "phone_number_id"
                    ]
                ),

            "phone_number":
                (
                    row[
                        "display_number"
                    ]
                    or row["e164"]
                    or ""
                ),

            "rule":
                row["rule"],

            "created_at":
                row[
                    "created_at"
                ].isoformat(),

            "updated_at":
                row[
                    "updated_at"
                ].isoformat(),
        }
        for row in rows
    ]


@router.put(
    "/upload-rules/{phone}"
)
def set_upload_rule(
    phone: str,
    body: UploadRuleUpdate,
    user: CurrentUser = Depends(
        require_owner
    ),
):

    rule = body.rule.upper().strip()

    if rule not in {
        "ALWAYS",
        "NEVER",
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                "Reguła musi mieć wartość "
                "ALWAYS albo NEVER."
            ),
        )

    with engine.begin() as connection:

        phone_row = _upsert_phone(
            connection,
            phone,
        )

        row = connection.execute(
            text(
                """
                INSERT INTO core.call_upload_rules (
                    phone_number_id,
                    rule
                )
                VALUES (
                    :phone_number_id,
                    :rule
                )
                ON CONFLICT (
                    phone_number_id
                )
                DO UPDATE SET
                    rule =
                        EXCLUDED.rule,
                    updated_at =
                        now()
                RETURNING
                    id,
                    rule,
                    created_at,
                    updated_at
                """
            ),
            {
                "phone_number_id":
                    phone_row["id"],

                "rule":
                    rule,
            },
        ).mappings().one()

    return {
        "id":
            str(
                row["id"]
            ),

        "phone_number":
            phone,

        "rule":
            row["rule"],

        "created_at":
            row[
                "created_at"
            ].isoformat(),

        "updated_at":
            row[
                "updated_at"
            ].isoformat(),
    }


@router.delete(
    "/upload-rules/{phone}"
)
def delete_upload_rule(
    phone: str,
    user: CurrentUser = Depends(
        require_owner
    ),
):

    with engine.begin() as connection:

        match_key = connection.execute(
            text(
                """
                SELECT core.phone_match_key(:phone)
                """
            ),
            {
                "phone":
                    phone,
            },
        ).scalar_one()

        deleted = connection.execute(
            text(
                """
                DELETE FROM core.call_upload_rules cur
                USING core.phone_numbers p
                WHERE cur.phone_number_id =
                    p.id
                  AND p.match_key =
                    :match_key
                RETURNING cur.id
                """
            ),
            {
                "match_key":
                    match_key,
            },
        ).first()

    return {
        "status":
            "ok",

        "deleted":
            deleted is not None,

        "phone_number":
            phone,
    }
