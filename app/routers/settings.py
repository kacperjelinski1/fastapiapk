from fastapi import APIRouter, Depends
from sqlalchemy import text
from app.database import engine
from app.security import CurrentUser, get_current_user

router=APIRouter(prefix="/settings",tags=["settings"])


@router.get("")
def get_settings(user:CurrentUser=Depends(get_current_user)):
    with engine.connect() as con:rows=con.execute(text("SELECT key,value,description FROM config.app_settings ORDER BY key")).mappings().all()
    return {r["key"]:{"value":r["value"],"description":r["description"] or ""} for r in rows}
