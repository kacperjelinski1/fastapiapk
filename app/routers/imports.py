from datetime import datetime
from typing import Any, Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.database import engine
from app.security import CurrentUser, require_owner

router=APIRouter(prefix="/imports",tags=["imports"])


class ImportContact(BaseModel):
    external_id:str|None=None
    phone_number:str=Field(min_length=1)
    display_name:str=""
    selected_contact:bool=False


class ImportCall(BaseModel):
    external_id:str|None=None
    phone_number:str=Field(min_length=1)
    direction:Literal["INCOMING","OUTGOING","UNKNOWN"]="UNKNOWN"
    started_at:datetime
    duration_seconds:int|None=Field(default=None,ge=0)


class PhoneBulkImport(BaseModel):
    source_device:str="ANDROID"
    contacts:list[ImportContact]=Field(default_factory=list)
    calls:list[ImportCall]=Field(default_factory=list)


def _phone(con,raw,source):
    return con.execute(text("""INSERT INTO core.phone_numbers(e164,display_number,first_seen_at,last_seen_at,source) VALUES(core.normalize_phone(:p),:p,now(),now(),:src) ON CONFLICT(e164) DO UPDATE SET display_number=EXCLUDED.display_number,last_seen_at=now() RETURNING id,match_key"""),{"p":raw,"src":source}).mappings().one()


def _client(con,pid,key,name,user_id):
    cid=con.execute(text("""SELECT c.id FROM core.clients c JOIN core.client_phones cp ON cp.client_id=c.id JOIN core.phone_numbers p ON p.id=cp.phone_number_id WHERE p.match_key=:k AND c.is_active=TRUE LIMIT 1"""),{"k":key}).scalar_one_or_none()
    merged=cid is not None
    if cid is None:
        cid=con.execute(text("INSERT INTO core.clients(display_name,created_by) VALUES(NULLIF(btrim(:n),''),:u) RETURNING id"),{"n":name,"u":user_id}).scalar_one()
        con.execute(text("INSERT INTO core.client_phones(client_id,phone_number_id,label,is_primary) VALUES(:c,:p,'Telefon',TRUE) ON CONFLICT DO NOTHING"),{"c":cid,"p":pid})
    elif name.strip():
        con.execute(text("UPDATE core.clients SET display_name=CASE WHEN display_name IS NULL OR btrim(display_name)='' THEN :n ELSE display_name END,updated_by=:u WHERE id=:c"),{"n":name.strip(),"u":user_id,"c":cid})
    return cid,merged


@router.post("/phone/bulk")
def import_phone(body:PhoneBulkImport,user:CurrentUser=Depends(require_owner)):
    with engine.begin() as con:
        batch=con.execute(text("INSERT INTO core.phone_import_batches(source_device,created_by) VALUES(:s,:u) RETURNING id"),{"s":body.source_device,"u":user.id}).scalar_one()
        unsaved=selected=calls=0
        for item in body.contacts:
            p=_phone(con,item.phone_number,"PHONE_IMPORT")
            cid,merged=_client(con,p["id"],p["match_key"],item.display_name,user.id)
            kind="SELECTED_CONTACT" if item.selected_contact else "UNSAVED_NUMBER"
            con.execute(text("""INSERT INTO core.phone_import_items(batch_id,item_type,external_id,phone_number_id,client_id,imported,merged,raw_payload) VALUES(:b,:t,:x,:p,:c,TRUE,:m,jsonb_build_object('display_name',:n))"""),{"b":batch,"t":kind,"x":item.external_id,"p":p["id"],"c":cid,"m":merged,"n":item.display_name})
            if item.selected_contact:selected+=1
            else:unsaved+=1
        for item in body.calls:
            p=_phone(con,item.phone_number,"PHONE_IMPORT")
            cid=con.execute(text("""SELECT c.id FROM core.clients c JOIN core.client_phones cp ON cp.client_id=c.id JOIN core.phone_numbers ph ON ph.id=cp.phone_number_id WHERE ph.match_key=:k LIMIT 1"""),{"k":p["match_key"]}).scalar_one_or_none()
            rid=con.execute(text("""SELECT so.id FROM service.service_orders so JOIN core.phone_numbers ph ON ph.id IN (so.primary_phone_id,so.secondary_phone_id) WHERE ph.match_key=:k ORDER BY (so.status IN ('IN_SERVICE','READY_FOR_PICKUP')) DESC,so.received_at DESC LIMIT 1"""),{"k":p["match_key"]}).scalar_one_or_none()
            call_id=con.execute(text("""INSERT INTO core.calls(phone_number_id,client_id,service_order_id,direction,started_at,duration_seconds,source) VALUES(:p,:c,:r,CAST(:d AS core.call_direction),:s,:dur,'PHONE_IMPORT') RETURNING id"""),{"p":p["id"],"c":cid,"r":rid,"d":item.direction,"s":item.started_at,"dur":item.duration_seconds}).scalar_one()
            con.execute(text("""INSERT INTO core.phone_import_items(batch_id,item_type,external_id,phone_number_id,client_id,imported,merged,raw_payload) VALUES(:b,'CALL',:x,:p,:c,TRUE,FALSE,jsonb_build_object('call_id',CAST(:call AS text)))"""),{"b":batch,"x":item.external_id,"p":p["id"],"c":cid,"call":call_id})
            calls+=1
        con.execute(text("""UPDATE core.phone_import_batches SET completed_at=now(),imported_unsaved_numbers=:u,imported_selected_contacts=:s,imported_calls=:c WHERE id=:b"""),{"u":unsaved,"s":selected,"c":calls,"b":batch})
    return {"batch_id":str(batch),"imported_unsaved_numbers":unsaved,"imported_selected_contacts":selected,"imported_calls":calls}
