import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.database import engine
from app.security import CurrentUser, require_owner

router=APIRouter(tags=["sms-reviews"])


def _uuid(v):
    try:return uuid.UUID(v)
    except ValueError as exc:raise HTTPException(400,"Nieprawidłowe ID.") from exc


def _phone(con,raw):
    return con.execute(text("""INSERT INTO core.phone_numbers(e164,display_number,first_seen_at,last_seen_at,source) VALUES(core.normalize_phone(:p),:p,now(),now(),'ANDROID_SMS') ON CONFLICT(e164) DO UPDATE SET display_number=EXCLUDED.display_number,last_seen_at=now() RETURNING id,match_key"""),{"p":raw}).mappings().one()


def _match(con,key):
    client=con.execute(text("""SELECT c.id FROM core.clients c JOIN core.client_phones cp ON cp.client_id=c.id JOIN core.phone_numbers p ON p.id=cp.phone_number_id WHERE p.match_key=:k AND c.is_active=TRUE LIMIT 1"""),{"k":key}).scalar_one_or_none()
    orders=con.execute(text("""SELECT so.id,so.reception_number FROM service.service_orders so JOIN core.phone_numbers p ON p.id IN (so.primary_phone_id,so.secondary_phone_id) WHERE p.match_key=:k AND so.status IN ('IN_SERVICE','READY_FOR_PICKUP') ORDER BY so.received_at DESC"""),{"k":key}).mappings().all()
    return client,orders


class SmsCreate(BaseModel):
    phone_number:str=Field(min_length=1)
    message_text:str=Field(min_length=1)
    direction:str="INCOMING"
    sent_received_at:datetime
    device_message_id:str|None=None
    reception_id:str|None=None


class ReviewCreate(BaseModel):
    phone_number:str=Field(min_length=1)
    reviewer_display_name:str=""
    review_reference:str=""
    note:str=""
    discount_percent:float=Field(default=10,ge=0,le=100)


class ReviewStatus(BaseModel):
    status:str
    note:str=""


@router.post("/sms")
def create_sms(body:SmsCreate,user:CurrentUser=Depends(require_owner)):
    d=body.direction.upper()
    if d not in {"INCOMING","OUTGOING","UNKNOWN"}:raise HTTPException(400,"Nieprawidłowy kierunek SMS.")
    with engine.begin() as con:
        p=_phone(con,body.phone_number);client,orders=_match(con,p["match_key"])
        rid=_uuid(body.reception_id) if body.reception_id else (orders[0]["id"] if len(orders)==1 else None)
        row=con.execute(text("""INSERT INTO core.sms_messages(phone_number_id,client_id,service_order_id,direction,message_text,sent_received_at,source,device_message_id) VALUES(:p,:c,:r,CAST(:d AS core.call_direction),:m,:t,'ANDROID_SMS',:x) ON CONFLICT(source,device_message_id) DO NOTHING RETURNING id"""),{"p":p["id"],"c":client,"r":rid,"d":d,"m":body.message_text,"t":body.sent_received_at,"x":body.device_message_id}).scalar_one_or_none()
    return {"id":str(row) if row else None,"client_id":str(client) if client else None,"reception_id":str(rid) if rid else None,"needs_reception_choice":len(orders)>1 and not body.reception_id}


@router.get("/sms")
def list_sms(reception_id:str|None=None,client_id:str|None=None,limit:int=Query(100,ge=1,le=500),user:CurrentUser=Depends(require_owner)):
    cond=[];params={"lim":limit}
    if reception_id:cond.append("s.service_order_id=:r");params["r"]=_uuid(reception_id)
    if client_id:cond.append("s.client_id=:c");params["c"]=_uuid(client_id)
    where=" WHERE "+" AND ".join(cond) if cond else ""
    with engine.connect() as con:
        rows=con.execute(text(f"""SELECT s.id,s.direction,s.message_text,s.sent_received_at,s.client_id,s.service_order_id,p.display_number FROM core.sms_messages s JOIN core.phone_numbers p ON p.id=s.phone_number_id{where} ORDER BY s.sent_received_at DESC LIMIT :lim"""),params).mappings().all()
    return [{"id":str(r["id"]),"direction":str(r["direction"]),"message_text":r["message_text"],"sent_received_at":r["sent_received_at"].isoformat(),"client_id":str(r["client_id"]) if r["client_id"] else None,"reception_id":str(r["service_order_id"]) if r["service_order_id"] else None,"phone_number":r["display_number"] or ""} for r in rows]


@router.post("/reviews")
def create_review(body:ReviewCreate,user:CurrentUser=Depends(require_owner)):
    with engine.begin() as con:
        p=_phone(con,body.phone_number);client,_=_match(con,p["match_key"])
        rid=con.execute(text("""INSERT INTO service.review_rewards(client_id,phone_number_id,reviewer_display_name,review_reference,status,discount_percent,note) VALUES(:c,:p,NULLIF(btrim(:n),''),NULLIF(btrim(:ref),''),'PENDING',:d,NULLIF(btrim(:note),'')) RETURNING id"""),{"c":client,"p":p["id"],"n":body.reviewer_display_name,"ref":body.review_reference,"d":body.discount_percent,"note":body.note}).scalar_one()
    return {"id":str(rid),"status":"PENDING"}


@router.get("/reviews")
def list_reviews(status:str|None=None,user:CurrentUser=Depends(require_owner)):
    params={};where=""
    if status:where=" WHERE rr.status=CAST(:s AS service.review_status)";params["s"]=status.upper()
    with engine.connect() as con:
        rows=con.execute(text(f"""SELECT rr.id,rr.status,rr.reviewer_display_name,rr.review_reference,rr.discount_percent,rr.received_at,rr.verified_at,rr.note,c.display_name,p.display_number FROM service.review_rewards rr JOIN core.phone_numbers p ON p.id=rr.phone_number_id LEFT JOIN core.clients c ON c.id=rr.client_id{where} ORDER BY rr.created_at DESC"""),params).mappings().all()
    return [{"id":str(r["id"]),"status":str(r["status"]),"reviewer_display_name":r["reviewer_display_name"] or "","review_reference":r["review_reference"] or "","discount_percent":float(r["discount_percent"] or 0),"received_at":r["received_at"].isoformat(),"verified_at":r["verified_at"].isoformat() if r["verified_at"] else None,"note":r["note"] or "","client_name":r["display_name"] or "","phone_number":r["display_number"] or ""} for r in rows]


@router.patch("/reviews/{review_id}")
def update_review(review_id:str,body:ReviewStatus,user:CurrentUser=Depends(require_owner)):
    rid=_uuid(review_id);s=body.status.upper()
    if s not in {"PENDING","VERIFIED","REJECTED"}:raise HTTPException(400,"Nieprawidłowy status opinii.")
    with engine.begin() as con:
        row=con.execute(text("""UPDATE service.review_rewards SET status=CAST(:s AS service.review_status),verified_at=CASE WHEN :s='VERIFIED' THEN now() ELSE NULL END,verified_by=CASE WHEN :s='VERIFIED' THEN :u ELSE NULL END,note=COALESCE(NULLIF(btrim(:n),''),note) WHERE id=:id RETURNING id"""),{"s":s,"u":user.id,"n":body.note,"id":rid}).first()
    if not row:raise HTTPException(404,"Nie znaleziono opinii.")
    return {"status":"ok","review_status":s}
