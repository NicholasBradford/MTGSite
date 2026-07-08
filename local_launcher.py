import os
import sys
import socket
import secrets
import webbrowser
from pathlib import Path
from threading import Timer

from waitress import serve


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_free_port(start=5000, stop=5100) -> int:
    for port in range(start, stop + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free local port found between 5000 and 5100.")


def configure_local_environment() -> Path:
    root = executable_dir()
    os.chdir(root)
    data_dir = root / "user_data"
    image_dir = data_dir / "img"
    logs_dir = data_dir / "logs"
    tcg_history_dir = data_dir / "tcg_history"
    tcgcsv_data_dir = data_dir / "tcgcsv"

    data_dir.mkdir(exist_ok=True)
    image_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)
    tcg_history_dir.mkdir(exist_ok=True)
    tcgcsv_data_dir.mkdir(exist_ok=True)

    secret_file = data_dir / ".secret_key"
    if not secret_file.exists():
        secret_file.write_text(secrets.token_hex(32), encoding="utf-8")

    os.environ.setdefault("SECRET_KEY", secret_file.read_text(encoding="utf-8").strip())
    os.environ.setdefault("DB_PATH", str(data_dir / "mtg_inventory.db"))
    os.environ.setdefault("IMAGE_PATH", str(image_dir))
    os.environ.setdefault("FLASK_DEBUG", "False")
    os.environ.setdefault("LOCAL_APP_MODE", "1")
    os.environ.setdefault("LOG_DIR", str(logs_dir))
    os.environ.setdefault("TCGCSV_HISTORY_DIR", str(tcg_history_dir))
    os.environ.setdefault("TCGCSV_LOCAL_TIMEZONE", "America/Chicago")

    return data_dir


def main():
    configure_local_environment()

    # Import only after env vars are set.
    from app import app

    port = find_free_port()
    url = f"http://127.0.0.1:{port}"

    Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"MTGSite is running locally at {url}")
    print("Close this window to stop the site.")

    serve(app, host="127.0.0.1", port=port, threads=8)


if __name__ == "__main__":
    main()