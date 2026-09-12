import os

import uvicorn

if __name__ == "__main__":
    # Render (and most PaaS) inject the real port via $PORT
    uvicorn.run("server:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", 8000)), log_level="info")
