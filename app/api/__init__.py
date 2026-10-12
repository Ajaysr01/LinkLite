from app.api.health import router as health_router
from app.api.links import router as links_router
from app.api.redirect import router as redirect_router

__all__ = ["health_router", "links_router", "redirect_router"]
