from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.database import engine
from app.security import CurrentUser, create_token, get_current_user, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordRequest(BaseModel):
    password: str = Field(min_length=6)


@router.post("/login")
def login(data: LoginRequest):
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT id,username,display_name,role,password_hash,is_active FROM core.app_users WHERE username=:u"),
            {"u": data.username},
        ).mappings().first()
        if row is None or not row["is_active"] or not verify_password(data.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Nieprawidłowy login lub hasło.")
        connection.execute(text("UPDATE core.app_users SET last_login_at=now() WHERE id=:id"), {"id": row["id"]})
    return {
        "token": create_token(str(row["id"]), row["username"], str(row["role"])),
        "user": {"id": str(row["id"]), "username": row["username"], "display_name": row["display_name"], "role": str(row["role"])},
    }


@router.get("/me")
def me(user: CurrentUser = Depends(get_current_user)):
    return user.__dict__


@router.post("/set-my-password")
def set_my_password(data: PasswordRequest, user: CurrentUser = Depends(get_current_user)):
    with engine.begin() as connection:
        connection.execute(text("UPDATE core.app_users SET password_hash=:p WHERE id=:id"), {"p": hash_password(data.password), "id": user.id})
    return {"status": "ok"}
