from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database.session import get_db
from database.models import User
from web.routes.auth import require_login
from core.config import detect_network_addresses

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/setup-guide", response_class=HTMLResponse)
async def setup_guide_view(
    request: Request,
    current_user: User = Depends(require_login),
):
    net_info = detect_network_addresses()
    return templates.TemplateResponse(
        request=request,
        name="setup_guide.html",
        context={
            "user": current_user,
            "net_info": net_info,
        },
    )


@router.get("/faq", response_class=HTMLResponse)
async def faq_page(
    request: Request,
    current_user: User = Depends(require_login),
):
    return templates.TemplateResponse(
        request=request,
        name="faq.html",
        context={"user": current_user},
    )
