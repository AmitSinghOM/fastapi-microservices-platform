"""Dependency-free, same-origin operational portal assets."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)
_ASSET_DIR = Path(__file__).resolve().parent.parent / "portal"
_CSP = (
    "default-src 'none'; base-uri 'none'; connect-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; img-src 'self'; "
    "script-src 'self'; style-src 'self'"
)


def _asset(
    name: str, media_type: str, *, document: bool = False
) -> FileResponse:
    headers = {"Cache-Control": "no-store"}
    if document:
        headers.update(
            {
                "Content-Security-Policy": _CSP,
                "Cross-Origin-Opener-Policy": "same-origin",
                "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
            }
        )
    return FileResponse(
        _ASSET_DIR / name,
        media_type=media_type,
        headers=headers,
    )


@router.get("/portal")
@router.get("/portal/")
async def portal() -> FileResponse:
    return _asset("index.html", "text/html", document=True)


@router.get("/portal/app.js")
async def portal_script() -> FileResponse:
    return _asset("app.js", "text/javascript")


@router.get("/portal/styles.css")
async def portal_styles() -> FileResponse:
    return _asset("styles.css", "text/css")
