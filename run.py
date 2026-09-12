import os

import uvicorn

if __name__ == "__main__":
    # Render (and most PaaS) inject the real port via $PORT
    kw = {}
    cert = os.environ.get("SSL_CERTFILE")
    key = os.environ.get("SSL_KEYFILE")
    if cert and key:
        # cloudflared tunnel origin = https://localhost:$PORT (Zero Trust
        # public hostname -> HTTPS origin); serve TLS with the cert the
        # host workflow generated and trusted.
        kw.update(ssl_certfile=cert, ssl_keyfile=key)
    uvicorn.run("server:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", 8000)), log_level="info", **kw)
