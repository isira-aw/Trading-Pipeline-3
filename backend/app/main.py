from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes_data import router as data_router

app = FastAPI(title="Automated Crypto Trading Pipeline", version="1.0.0")

# Allow CORS for Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, restrict this
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/status")
async def get_status():
    return {"status": "ok", "message": "API is running"}

app.include_router(data_router, prefix="/api/data", tags=["Data"])
