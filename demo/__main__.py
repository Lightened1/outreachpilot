"""Run the demo portal: ``python -m demo`` (host/port from DEMO_HOST / DEMO_PORT)."""

import os

import uvicorn

from demo.app import create_demo_app

if __name__ == "__main__":
    uvicorn.run(
        create_demo_app(),
        host=os.getenv("DEMO_HOST", "127.0.0.1"),
        port=int(os.getenv("DEMO_PORT", "8010")),
        log_level=os.getenv("DEMO_LOG_LEVEL", "warning"),
    )
