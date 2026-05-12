from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
import uvicorn

from src.config import load_config
from src.utils.logging_config import setup_logging
from src.api.routes import router
from src.api.task_routes import task_router

# Load config and setup logging early
config = load_config()
setup_logging(
    level=config.logging.level,
    log_format=config.logging.format,
    rotation=config.logging.rotation,
    retention=config.logging.retention,
)

app = FastAPI(
    title="Fund-Advisor API",
    description="API for the Fund-Advisor frontend",
    version="0.1.0",
)

# Allow CORS for local development (Vite default port is usually 5173 or similar)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For production, specify the exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")
app.include_router(task_router, prefix="/api")

@app.get("/health")
def health_check():
    return {"status": "ok"}

def run_server():
    """Run the API server."""
    logger.info("Starting FastAPI server...")
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=8000, reload=True)

if __name__ == "__main__":
    run_server()
