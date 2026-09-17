import hashlib
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.config import settings
from app.database import engine
from app.security import CurrentUser, require_owner

router=APIRouter(prefix="/calls",tags=["calls"])


def _uuid(v,label="ID"):
    try:return uuid.UUID(v)
    except ValueError as exc:raise HTTPException(400,f"Nieprawidłowe {label}.") from exc


def _upsert_phone(con,phone):
    return con.execute(text("""INSERT INTO core.phone_numbers(e164,display_number,first_seen_at,last_seen_at,source) VALUES(core.normalize_phone(:p),:p,now(),now(),'ANDROID_CALL') ON CONFLICT(e164) DO UPDATE SET display_number=EXCLUDED.display_number,last_seen_at=now() RETURNING id,match_key,e164"""),{"p":phone}).mappings().one()


def _match(con,match_key):
    client=con.execute(text("""SELECT c.id FROM core.clients c JOIN core.client_phones cp ON cp.client_id=c.id JOIN core.phone_numbers p ON p.id=cp.phone_number_id WHERE p.match_key=:k AND c.is_active=TRUE LIMIT 1"""),{"k":match_key}).scalar_one_or_none()
    orders=con.execute(text("""SELECT so.id,so.reception_number FROM service.service_orders so JOIN core.phone_numbers p ON p.id IN (so.primary_phone_id,so.secondary_phone_id) WHERE p.match_key=:k AND so.status IN ('IN_SERVICE','READY_FOR_PICKUP') ORDER BY so.received_at DESC"""),{"k":match_key}).mappings().all()
    return client,orders


class CallCreate(BaseModel):
    phone_number:str=Field(min_length=1)
    direction:str="UNKNOWN"
    started_at:datetime
    duration_seconds:int|None=Field(default=None,ge=0)
    source:str="ANDROID_DIALER"
    reception_id:str|None=None


@router.get("")
def list_calls(reception_id:str|None=None,client_id:str|None=None,phone:str|None=None,limit:int=Query(100,ge=1,le=500),user:CurrentUser=Depends(require_owner)):
    conditions=[];params={"lim":limit}
    if reception_id:conditions.append("c.service_order_id=:rid");params["rid"]=_uuid(reception_id)
    if client_id:conditions.append("c.client_id=:cid");params["cid"]=_uuid(client_id)
    if phone:conditions.append("p.match_key=core.phone_match_key(:phone)");params["phone"]=phone
    where=" WHERE "+" AND ".join(conditions) if conditions else ""
    with engine.connect() as con:
        rows=con.execute(text(f"""SELECT c.id,c.direction,c.started_at,c.duration_seconds,c.source,c.client_id,c.service_order_id,p.display_number,p.e164,(SELECT count(*) FROM core.call_recordings r WHERE r.call_id=c.id) recording_count FROM core.calls c JOIN core.phone_numbers p ON p.id=c.phone_number_id{where} ORDER BY c.started_at DESC LIMIT :lim"""),params).mappings().all()
    return [{"id":str(r["id"]),"direction":str(r["direction"]),"started_at":r["started_at"].isoformat(),"duration_seconds":r["duration_seconds"],"source":r["source"],"client_id":str(r["client_id"]) if r["client_id"] else None,"reception_id":str(r["service_order_id"]) if r["service_order_id"] else None,"phone_number":r["display_number"] or r["e164"],"recording_count":r["recording_count"]} for r in rows]


@router.get("/match/{phone}")
def match_phone(phone:str,user:CurrentUser=Depends(require_owner)):
    with engine.connect() as con:
        key=con.execute(text("SELECT core.phone_match_key(:p)"),{"p":phone}).scalar_one()
        client,orders=_match(con,key)
    return {"client_id":str(client) if client else None,"active_receptions":[{"id":str(o["id"]),"reception_number":o["reception_number"]} for o in orders],"auto_attach_reception_id":str(orders[0]["id"]) if len(orders)==1 else None}


@router.post("")
def create_call(body:CallCreate,user:CurrentUser=Depends(require_owner)):
    direction=body.direction.upper()
    if direction not in {"INCOMING","OUTGOING","UNKNOWN"}:raise HTTPException(400,"Nieprawidłowy kierunek rozmowy.")
    with engine.begin() as con:
        p=_upsert_phone(con,body.phone_number);client,orders=_match(con,p["match_key"])
        rid=_uuid(body.reception_id) if body.reception_id else (orders[0]["id"] if len(orders)==1 else None)
        cid=client
        row=con.execute(text("""INSERT INTO core.calls(phone_number_id,client_id,service_order_id,direction,started_at,duration_seconds,source) VALUES(:p,:c,:r,CAST(:d AS core.call_direction),:s,:dur,:src) RETURNING id"""),{"p":p["id"],"c":cid,"r":rid,"d":direction,"s":body.started_at,"dur":body.duration_seconds,"src":body.source}).scalar_one()
    return {"id":str(row),"client_id":str(cid) if cid else None,"reception_id":str(rid) if rid else None,"needs_reception_choice":len(orders)>1 and body.reception_id is None}


@router.patch("/{call_id}/attach/{reception_id}")
def attach_call(call_id:str,reception_id:str,user:CurrentUser=Depends(require_owner)):
    cid=_uuid(call_id);rid=_uuid(reception_id)
    with engine.begin() as con:
        client=con.execute(text("SELECT client_id FROM service.service_orders WHERE id=:id"),{"id":rid}).scalar_one_or_none()
        row=con.execute(text("UPDATE core.calls SET service_order_id=:r,client_id=COALESCE(client_id,:c) WHERE id=:id RETURNING id"),{"r":rid,"c":client,"id":cid}).first()
    if not row:raise HTTPException(404,"Nie znaleziono rozmowy.")
    return {"status":"ok"}


@router.post("/{call_id}/recording")
def upload_recording(call_id:str,file:UploadFile=File(...),user:CurrentUser=Depends(require_owner)):
    cid=_uuid(call_id);root=Path(settings.media_root).resolve();folder=root/"calls"/str(cid);folder.mkdir(parents=True,exist_ok=True)
    original=file.filename or "recording.mp3";suffix=Path(original).suffix.lower() or ".mp3";path=folder/f"{uuid.uuid4()}{suffix}";sha=hashlib.sha256();size=0
    try:
        with path.open("wb") as out:
            while True:
                chunk=file.file.read(1024*1024)
                if not chunk:break
                out.write(chunk);sha.update(chunk);size+=len(chunk)
        if not size:raise HTTPException(400,"Pusty plik.")
        key=str(path.relative_to(root)).replace("\\","/")
        with engine.begin() as con:
            call=con.execute(text("SELECT p.e164,c.started_at FROM core.calls c JOIN core.phone_numbers p ON p.id=c.phone_number_id WHERE c.id=:id"),{"id":cid}).mappings().first()
            if not call:raise HTTPException(404,"Nie znaleziono rozmowy.")
            sid=con.execute(text("""INSERT INTO core.storage_objects(storage_area,object_key,original_filename,mime_type,extension,size_bytes,sha256,uploaded_at,upload_completed,created_by) VALUES('calls',:k,:o,:m,:e,:s,:h,now(),TRUE,:u) RETURNING id"""),{"k":key,"o":original,"m":file.content_type or "audio/mpeg","e":suffix,"s":size,"h":sha.hexdigest(),"u":user.id}).scalar_one()
            rid=con.execute(text("""INSERT INTO core.call_recordings(call_id,storage_object_id,original_filename,parsed_phone_e164,parsed_started_at,import_source) VALUES(:c,:s,:o,:p,:st,'ANDROID') RETURNING id"""),{"c":cid,"s":sid,"o":original,"p":call["e164"],"st":call["started_at"]}).scalar_one()
        return {"id":str(rid),"size_bytes":size,"content_url":f"/calls/recordings/{rid}/content"}
    except Exception:
        if path.exists():path.unlink(missing_ok=True)
        raise
    finally:file.file.close()


@router.get("/recordings/{recording_id}/content")
def recording_content(recording_id:str,user:CurrentUser=Depends(require_owner)):
    rid=_uuid(recording_id)
    with engine.connect() as con:
        row=con.execute(text("SELECT so.object_key,so.original_filename,so.mime_type FROM core.call_recordings cr JOIN core.storage_objects so ON so.id=cr.storage_object_id WHERE cr.id=:id AND so.deleted_at IS NULL"),{"id":rid}).mappings().first()
    if not row:raise HTTPException(404,"Nie znaleziono nagrania.")
    root=Path(settings.media_root).resolve();path=(root/row["object_key"]).resolve()
    if not path.is_file():raise HTTPException(404,"Brak pliku na dysku.")
    return FileResponse(path,media_type=row["mime_type"] or "audio/mpeg",filename=row["original_filename"] or path.name)

@router.get("/recordings")
def list_recordings(reception_id:str|None=None,client_id:str|None=None,limit:int=Query(200,ge=1,le=1000),user:CurrentUser=Depends(require_owner)):
    cond=[];params={"lim":limit}
    if reception_id:cond.append("c.service_order_id=:r");params["r"]=_uuid(reception_id)
    if client_id:cond.append("c.client_id=:c");params["c"]=_uuid(client_id)
    where=" WHERE "+" AND ".join(cond) if cond else ""
    with engine.connect() as con:
        rows=con.execute(text(f"""SELECT cr.id,cr.original_filename,cr.imported_at,c.id call_id,c.started_at,c.direction,c.service_order_id,c.client_id,p.display_number,so.size_bytes,so.mime_type FROM core.call_recordings cr JOIN core.calls c ON c.id=cr.call_id JOIN core.phone_numbers p ON p.id=c.phone_number_id JOIN core.storage_objects so ON so.id=cr.storage_object_id{where} ORDER BY c.started_at DESC LIMIT :lim"""),params).mappings().all()
    return [{"id":str(r["id"]),"call_id":str(r["call_id"]),"original_filename":r["original_filename"],"started_at":r["started_at"].isoformat(),"direction":str(r["direction"]),"reception_id":str(r["service_order_id"]) if r["service_order_id"] else None,"client_id":str(r["client_id"]) if r["client_id"] else None,"phone_number":r["display_number"] or "","size_bytes":r["size_bytes"] or 0,"mime_type":r["mime_type"] or "audio/mpeg","content_url":f"/calls/recordings/{r['id']}/content"} for r in rows]


@router.post("/import-recording")
def import_recording(
    phone_number:str=Form(...),
    started_at:datetime=Form(...),
    direction:str=Form("UNKNOWN"),
    duration_seconds:int|None=Form(None),
    reception_id:str|None=Form(None),
    file:UploadFile=File(...),
    user:CurrentUser=Depends(require_owner),
):
    call=create_call(CallCreate(phone_number=phone_number,direction=direction,started_at=started_at,duration_seconds=duration_seconds,source="PHONE_IMPORT",reception_id=reception_id),user)
    result=upload_recording(call["id"],file,user)
    return {"call":call,"recording":result}
