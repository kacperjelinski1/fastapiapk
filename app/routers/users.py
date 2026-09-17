import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.database import engine
from app.security import CurrentUser, hash_password, require_owner

router=APIRouter(prefix="/users",tags=["users"])


class UserCreate(BaseModel):
    username:str=Field(min_length=2,max_length=100)
    display_name:str=Field(min_length=1,max_length=200)
    role:str="RECEPTION"
    password:str=Field(min_length=6)


class UserUpdate(BaseModel):
    display_name:str|None=None
    role:str|None=None
    is_active:bool|None=None
    password:str|None=Field(default=None,min_length=6)


def _uuid(v):
    try:return uuid.UUID(v)
    except ValueError as exc:raise HTTPException(400,"Nieprawidłowe ID użytkownika.") from exc


def _role(v):
    v=v.upper()
    if v not in {"OWNER","ADMIN","RECEPTION","TECHNICIAN","READONLY"}:raise HTTPException(400,"Nieprawidłowa rola.")
    return v


@router.get("")
def list_users(user:CurrentUser=Depends(require_owner)):
    with engine.connect() as con:rows=con.execute(text("SELECT id,username,display_name,email,role,is_active,created_at,last_login_at FROM core.app_users ORDER BY created_at")).mappings().all()
    return [{**dict(r),"id":str(r["id"]),"role":str(r["role"]),"created_at":r["created_at"].isoformat(),"last_login_at":r["last_login_at"].isoformat() if r["last_login_at"] else None} for r in rows]


@router.post("")
def create_user(body:UserCreate,user:CurrentUser=Depends(require_owner)):
    role=_role(body.role)
    try:
        with engine.begin() as con:
            uid=con.execute(text("""INSERT INTO core.app_users(username,display_name,role,password_hash) VALUES(:u,:n,CAST(:r AS core.app_role),:p) RETURNING id"""),{"u":body.username,"n":body.display_name,"r":role,"p":hash_password(body.password)}).scalar_one()
    except Exception as exc:raise HTTPException(400,f"Nie udało się utworzyć użytkownika: {exc}") from exc
    return {"id":str(uid)}


@router.patch("/{user_id}")
def update_user(user_id:str,body:UserUpdate,user:CurrentUser=Depends(require_owner)):
    uid=_uuid(user_id);sets=[];params={"id":uid}
    if body.display_name is not None:sets.append("display_name=:n");params["n"]=body.display_name
    if body.role is not None:sets.append("role=CAST(:r AS core.app_role)");params["r"]=_role(body.role)
    if body.is_active is not None:sets.append("is_active=:a");params["a"]=body.is_active
    if body.password is not None:sets.append("password_hash=:p");params["p"]=hash_password(body.password)
    if not sets:return {"status":"ok"}
    with engine.begin() as con:row=con.execute(text(f"UPDATE core.app_users SET {','.join(sets)} WHERE id=:id RETURNING id"),params).first()
    if not row:raise HTTPException(404,"Nie znaleziono użytkownika.")
    return {"status":"ok"}
