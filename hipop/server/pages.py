"""
HIPOP 工作台 - HTML 页面路由 (Jinja2)
"""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
import os

from . import auth as _auth_mod

HIPOP_ROOT = os.path.dirname(os.path.dirname(__file__))
templates = Jinja2Templates(directory=os.path.join(HIPOP_ROOT, "server", "templates"))

router = APIRouter()


def _ctx(request, store: str, **extra):
    user = _auth_mod.get_current_user(request)
    base = {
        "request": request,
        "store": store.upper(),
        "store_lower": store.lower(),
        "store_full": f"HIPOP-NOON-{store.upper()}",
        "current_user": user.get("display_name") or user.get("email", "Cherry"),
        "current_role": user.get("role", "ops"),
        "current_user_obj": user,
        "is_default_user": user.get("is_default", False),
        "available_stores": [
            {"code": "KSA", "name": "HIPOP-NOON-KSA"},
            {"code": "UAE", "name": "HIPOP-NOON-UAE"},
        ],
    }
    base.update(extra)
    return base


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "mode": "login"})


@router.get("/register")
def register_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "mode": "register"})


@router.get("/onboarding")
def onboarding_page(request: Request):
    user = _auth_mod.get_current_user(request)
    if user.get("is_default"):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("onboarding.html", {
        "request": request,
        "current_user_obj": user,
    })


@router.get("/")
def overview(request: Request, store: str = "ksa"):
    return templates.TemplateResponse("overview.html", _ctx(request, store, page="overview"))


@router.get("/module/sales")
def module_sales(request: Request, store: str = "ksa"):
    return templates.TemplateResponse("module_sales.html", _ctx(request, store, page="sales"))


@router.get("/module/logistics")
def module_logistics(request: Request, store: str = "ksa"):
    return templates.TemplateResponse("module_logistics.html", _ctx(request, store, page="logistics"))


@router.get("/module/replenish")
def module_replenish(request: Request, store: str = "ksa"):
    return templates.TemplateResponse("module_replenish.html", _ctx(request, store, page="replenish"))


@router.get("/module/selection")
def module_selection(request: Request, store: str = "ksa"):
    return templates.TemplateResponse("module_selection.html", _ctx(request, store, page="selection"))


@router.get("/module/feishu")
def module_feishu(request: Request, store: str = "ksa"):
    return templates.TemplateResponse("module_feishu.html", _ctx(request, store, page="feishu"))


@router.get("/role/liuhe")
def role_liuhe(request: Request):
    """职能模式：刘鹤视图（跨店物流跟单）"""
    return templates.TemplateResponse("role_liuhe.html", _ctx(request, "all", page="liuhe", current_user="刘鹤", current_role="跟单"))
