"""
TradingView Webhook Bot – entrypoint.
Run: python -m src.main
"""
import uvicorn

from src.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "src.server:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
