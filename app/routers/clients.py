import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.database import engine
from app.security import CurrentUser, require_staff

router = APIRouter(prefix="/clients", tags=["clients"])


def _uuid(value: str):
    try: return uuid.UUID(value)
    except ValueError as exc: raise HTTPException(400, "Nieprawidłowe ID klienta.") from exc


class ClientUpdate(BaseModel):
    display_name: str = ""
    email: str = ""
    address: str = ""
    notes: str = ""


class PhoneAdd(BaseModel):
    phone: str = Field(min_length=1)
    label: str = "Telefon"
    is_primary: bool = False


@router.get("")
def list_clients(search: str = Query(default="", max_length=200), user: CurrentUser = Depends(require_staff)):
    params = {}
    where = "WHERE c.is_active=TRUE"
    if search.strip():
        params["q"] = f"%{search.strip()}%"
        where += " AND (COALESCE(c.display_name,'') ILIKE :q OR EXISTS (SELECT 1 FROM core.client_phones cp JOIN core.phone_numbers p ON p.id=cp.phone_number_id WHERE cp.client_id=c.id AND (p.display_number ILIKE :q OR p.e164 ILIKE :q)))"
    with engine.connect() as con:
        rows = con.execute(text(f"""
            SELECT c.id,c.display_name,c.email,c.created_at,
                   COALESCE((SELECT p.display_number FROM core.client_phones cp JOIN core.phone_numbers p ON p.id=cp.phone_number_id WHERE cp.client_id=c.id ORDER BY cp.is_primary DESC,cp.created_at LIMIT 1),'') phone,
                   (SELECT count(*) FROM service.service_orders so WHERE so.client_id=c.id) reception_count
            FROM core.clients c {where} ORDER BY c.updated_at DESC LIMIT 500
        """), params).mappings().all()
    return [{**dict(r), "id":str(r["id"]), "created_at":r["created_at"].isoformat()} for r in rows]


@router.get("/by-phone/{phone}")
def by_phone(phone: str, user: CurrentUser = Depends(require_staff)):
    with engine.connect() as con:
        row = con.execute(text("""
            SELECT c.id,c.display_name,c.email,c.address,c.notes
            FROM core.clients c JOIN core.client_phones cp ON cp.client_id=c.id JOIN core.phone_numbers p ON p.id=cp.phone_number_id
            WHERE p.match_key=core.phone_match_key(:phone) AND c.is_active=TRUE LIMIT 1
        """), {"phone":phone}).mappings().first()
    if not row: raise HTTPException(404,"Nie znaleziono klienta.")
    return {**dict(row),"id":str(row["id"])}


@router.get("/{client_id}")
def get_client(client_id: str, user: CurrentUser = Depends(require_staff)):
    cid=_uuid(client_id)
    with engine.connect() as con:
        c=con.execute(text("SELECT id,display_name,email,address,notes,created_at,updated_at FROM core.clients WHERE id=:id"),{"id":cid}).mappings().first()
        if not c: raise HTTPException(404,"Nie znaleziono klienta.")
        phones=con.execute(text("SELECT p.id,p.e164,p.display_number,cp.label,cp.is_primary FROM core.client_phones cp JOIN core.phone_numbers p ON p.id=cp.phone_number_id WHERE cp.client_id=:id ORDER BY cp.is_primary DESC,cp.created_at"),{"id":cid}).mappings().all()
        devices=con.execute(text("SELECT id,device_type,manufacturer,model,serial_number,hostname,os,os_version,breeze_device_id,last_seen,is_online FROM core.devices WHERE client_id=:id AND archived_at IS NULL ORDER BY updated_at DESC"),{"id":cid}).mappings().all()
        orders=con.execute(text("SELECT id,reception_number,status,received_at FROM service.service_orders WHERE client_id=:id ORDER BY received_at DESC"),{"id":cid}).mappings().all()
    return {
        "id":str(c["id"]),"display_name":c["display_name"] or "","email":c["email"] or "","address":c["address"] or "","notes":c["notes"] or "",
        "created_at":c["created_at"].isoformat(),"updated_at":c["updated_at"].isoformat(),
        "phones":[{**dict(x),"id":str(x["id"])} for x in phones],
        "devices":[{**dict(x),"id":str(x["id"]),"last_seen":x["last_seen"].isoformat() if x["last_seen"] else None} for x in devices],
        "receptions":[{"id":str(x["id"]),"reception_number":x["reception_number"],"status":str(x["status"]),"received_at":x["received_at"].isoformat()} for x in orders],
    }


@router.patch("/{client_id}")
def update_client(client_id: str, body: ClientUpdate, user: CurrentUser = Depends(require_staff)):
    cid=_uuid(client_id)
    with engine.begin() as con:
        row=con.execute(text("""UPDATE core.clients SET display_name=NULLIF(btrim(:n),''),email=NULLIF(btrim(:e),''),address=NULLIF(btrim(:a),''),notes=NULLIF(btrim(:notes),''),updated_by=:u WHERE id=:id RETURNING id"""),{"n":body.display_name,"e":body.email,"a":body.address,"notes":body.notes,"u":user.id,"id":cid}).first()
    if not row: raise HTTPException(404,"Nie znaleziono klienta.")
    return {"status":"ok"}


@router.post("/{client_id}/phones")
def add_phone(client_id: str, body: PhoneAdd, user: CurrentUser = Depends(require_staff)):
    cid=_uuid(client_id)
    with engine.begin() as con:
        pid=con.execute(text("""INSERT INTO core.phone_numbers(e164,display_number,first_seen_at,last_seen_at,source) VALUES(core.normalize_phone(:p),:p,now(),now(),'ANDROID') ON CONFLICT(e164) DO UPDATE SET display_number=EXCLUDED.display_number,last_seen_at=now() RETURNING id"""),{"p":body.phone}).scalar_one()
        if body.is_primary: con.execute(text("UPDATE core.client_phones SET is_primary=FALSE WHERE client_id=:id"),{"id":cid})
        con.execute(text("""INSERT INTO core.client_phones(client_id,phone_number_id,label,is_primary) VALUES(:c,:p,:l,:pr) ON CONFLICT(client_id,phone_number_id) DO UPDATE SET label=EXCLUDED.label,is_primary=EXCLUDED.is_primary"""),{"c":cid,"p":pid,"l":body.label,"pr":body.is_primary})
    return {"status":"ok","phone_id":str(pid)}
