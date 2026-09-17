import uuid
from datetime import date
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.database import engine
from app.security import CurrentUser, require_owner

router=APIRouter(tags=["owner-finances"])


def _uuid(v):
    try:return uuid.UUID(v)
    except ValueError as exc:raise HTTPException(400,"Nieprawidłowe ID.") from exc


class FinanceBody(BaseModel):
    service_amount: Decimal = Field(default=0, ge=0)
    material_cost: Decimal = Field(default=0, ge=0)
    donor_material_value: Decimal = Field(default=0, ge=0)


@router.get("/receptions/{reception_id}/finances")
def get_finances(reception_id:str,user:CurrentUser=Depends(require_owner)):
    rid=_uuid(reception_id)
    with engine.connect() as con:
        row=con.execute(text("SELECT service_amount,material_cost,donor_material_value,actual_profit,economic_profit FROM service.v_owner_profit WHERE service_order_id=:id"),{"id":rid}).mappings().first()
    if not row: raise HTTPException(404,"Nie znaleziono przyjęcia.")
    return {k:float(v or 0) for k,v in row.items()}


@router.put("/receptions/{reception_id}/finances")
def put_finances(reception_id:str,body:FinanceBody,user:CurrentUser=Depends(require_owner)):
    rid=_uuid(reception_id)
    with engine.begin() as con:
        exists=con.execute(text("SELECT 1 FROM service.service_orders WHERE id=:id"),{"id":rid}).scalar_one_or_none()
        if not exists:raise HTTPException(404,"Nie znaleziono przyjęcia.")
        con.execute(text("""INSERT INTO service.owner_finances(service_order_id,service_amount,material_cost,donor_material_value,created_by,updated_by) VALUES(:id,:s,:m,:d,:u,:u) ON CONFLICT(service_order_id) DO UPDATE SET service_amount=EXCLUDED.service_amount,material_cost=EXCLUDED.material_cost,donor_material_value=EXCLUDED.donor_material_value,updated_by=EXCLUDED.updated_by"""),{"id":rid,"s":body.service_amount,"m":body.material_cost,"d":body.donor_material_value,"u":user.id})
    return get_finances(reception_id,user)


@router.get("/reports/finances")
def finance_report(date_from:date|None=Query(None),date_to:date|None=Query(None),group_by:str=Query("month",pattern="^(month|quarter|year)$"),user:CurrentUser=Depends(require_owner)):
    trunc={"month":"month","quarter":"quarter","year":"year"}[group_by]
    conditions=[];params={}
    if date_from:conditions.append("received_at >= :f");params["f"]=date_from
    if date_to:conditions.append("received_at < (CAST(:t AS date) + interval '1 day')");params["t"]=date_to
    where=" WHERE "+" AND ".join(conditions) if conditions else ""
    with engine.connect() as con:
        rows=con.execute(text(f"""SELECT date_trunc('{trunc}',received_at) period,count(*) orders,sum(service_amount) service_amount,sum(material_cost) material_cost,sum(donor_material_value) donor_material_value,sum(actual_profit) actual_profit,sum(economic_profit) economic_profit FROM service.v_owner_profit{where} GROUP BY 1 ORDER BY 1 DESC"""),params).mappings().all()
    return [{"period":r["period"].isoformat(),**{k:(int(v) if k=="orders" else float(v or 0)) for k,v in r.items() if k!="period"}} for r in rows]
