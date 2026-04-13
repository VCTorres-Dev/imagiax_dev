"""
app.core.templates — Shared Jinja2 templates instance.

Single root at app/templates/ with domain subdirectories:
    admin/     — Admin SPA, login, forbidden pages
    pagos/     — Checkout, history, result pages
    informes/  — Report viewer

Usage:
    from app.core.templates import templates

    @router.get("/admin")
    async def admin_panel(request: Request):
        return templates.TemplateResponse("admin/admin_spa.html", {"request": request})

Directory resolution:
    - The path "app/templates" is relative to the process CWD.
    - In Docker the entrypoint is `uvicorn app.main:create_app --factory` from /app/,
      so "app/templates" → /app/app/templates. That matches the Dockerfile COPY target.
    - In local dev, CWD is the project root, so "app/templates" → <project>/app/templates.
    Both cases resolve correctly without any path manipulation.
"""

from starlette.templating import Jinja2Templates

# One instance shared across all domain routers.
# Jinja2 template lookup is: directory / "domain/template.html"
templates = Jinja2Templates(directory="app/templates")
