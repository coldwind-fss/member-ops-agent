import os

from uvicorn import Config, Server

from .database import run_with_database_loop


def main() -> int:
    port = int(os.getenv("PRIVATE_OPS_PORT", "8000"))
    server = Server(Config("app.main:app", host="127.0.0.1", port=port))
    run_with_database_loop(server.serve())
    return 0 if server.started else 1


if __name__ == "__main__":
    raise SystemExit(main())
