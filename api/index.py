"""
Vercel entry point.  Vercel's Python runtime looks for an ASGI/WSGI app
exported from a file under api/.  We just re-export the FastAPI app.
"""
from app.main import app  # noqa: F401
