import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from app.database import engine
from app.security import CurrentUser, require_owner

router=APIRouter(prefix="/assist",tags=["assist-read"])


def _uuid(v):
    try:return uuid.UUID(v)
    except ValueError as exc:raise HTTPException(400,"Nieprawidłowe ID klienta.") from exc


@router.get("/client/{client_id}")
def client_assist(client_id:str,user:CurrentUser=Depends(require_owner)):
    cid=_uuid(client_id)
    with engine.connect() as con:
        installs=con.execute(text("""SELECT ai.id,ai.device_id,ai.computer_name,ai.os_name,ai.os_version,ai.app_version,ai.last_seen_at,ai.is_active,d.breeze_device_id FROM assist.assist_installations ai LEFT JOIN core.devices d ON d.id=ai.device_id WHERE ai.client_id=:c ORDER BY ai.updated_at DESC"""),{"c":cid}).mappings().all()
        licenses=con.execute(text("""SELECT l.id,l.license_key,l.tier,l.status,l.valid_from,l.valid_until,l.max_devices,la.installation_id FROM assist.license_assignments la JOIN assist.licenses l ON l.id=la.license_id WHERE la.client_id=:c AND la.revoked_at IS NULL ORDER BY la.assigned_at DESC"""),{"c":cid}).mappings().all()
        alerts=con.execute(text("""SELECT a.id,a.installation_id,a.severity,a.alert_code,a.title,a.message,a.first_seen_at,a.last_seen_at,a.resolved_at FROM assist.assist_alerts a JOIN assist.assist_installations i ON i.id=a.installation_id WHERE i.client_id=:c ORDER BY (a.resolved_at IS NULL) DESC,a.last_seen_at DESC LIMIT 100"""),{"c":cid}).mappings().all()
    return {
        "installations":[{**dict(r),"id":str(r["id"]),"device_id":str(r["device_id"]) if r["device_id"] else None,"last_seen_at":r["last_seen_at"].isoformat() if r["last_seen_at"] else None} for r in installs],
        "licenses":[{**dict(r),"id":str(r["id"]),"tier":str(r["tier"]),"status":str(r["status"]),"installation_id":str(r["installation_id"]) if r["installation_id"] else None,"valid_from":r["valid_from"].isoformat(),"valid_until":r["valid_until"].isoformat() if r["valid_until"] else None} for r in licenses],
        "alerts":[{"id":str(r["id"]),"installation_id":str(r["installation_id"]),"severity":str(r["severity"]),"alert_code":r["alert_code"],"title":r["title"],"message":r["message"],"first_seen_at":r["first_seen_at"].isoformat(),"last_seen_at":r["last_seen_at"].isoformat(),"resolved_at":r["resolved_at"].isoformat() if r["resolved_at"] else None} for r in alerts],
    }
