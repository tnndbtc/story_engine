"""
story_engine — FastAPI application entry point.

Usage:
    uvicorn main:app --host 0.0.0.0 --port 8003 --reload

Serves the REST API for generated stories.
"""

import logging
import os
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Add src/ to path so imports work
sys.path.insert(0, os.path.dirname(__file__))

from api.routes import router
from db.models import init_db

# Logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(levelname)s %(asctime)s %(name)s %(message)s',
)
logger = logging.getLogger(__name__)

# Initialize story_engine's own database
init_db()

app = FastAPI(
    title="Global Signal Radar — Story Engine",
    description="REST API for AI-generated news scripts based on crawled trend data.",
    version="0.1.0",
)

# CORS — allow trend_ui to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/")
def root():
    return {
        "service": "story_engine",
        "version": "0.1.0",
        "docs": "/docs",
    }
