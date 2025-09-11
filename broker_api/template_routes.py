

from fastapi import Request
from fastapi.templating import Jinja2Templates
from main import app  # Import the FastAPI app instance

templates = Jinja2Templates(directory="templates")

@app.get("/login-page")
async def get_login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/profile-page")
async def get_profile_page(request: Request):
    return templates.TemplateResponse("profile.html", {"request": request})

# Add more routes as needed
