"""Run the GitModel Hub server: ``python -m hub``."""
from __future__ import annotations

import uvicorn

from .config import get_settings


def main() -> None:
    s = get_settings()
    print(f"GitModel Hub → http://{s.host}:{s.port}  (data: {s.data_dir})")
    uvicorn.run("hub.server:app", host=s.host, port=s.port, log_level="info")


if __name__ == "__main__":
    main()
