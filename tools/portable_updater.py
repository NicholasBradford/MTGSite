# tools/portable_updater.py

from pathlib import Path
import shutil
import datetime
import sys

PRESERVE_NAMES = {
    ".env",
    "var",
    "instance",
    "tcg_history",
    "logs",
}

PAYLOAD_DIR_NAME = "update_payload"


def timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def copy_tree(src: Path, dst: Path):
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()

    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def backup_existing_install(install_dir: Path) -> Path:
    backup_dir = install_dir / f"backup_before_update_{timestamp()}"
    backup_dir.mkdir(parents=True, exist_ok=False)

    for item in install_dir.iterdir():
        if item.name == PAYLOAD_DIR_NAME:
            continue
        if item.name.startswith("backup_before_update_"):
            continue

        target = backup_dir / item.name

        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

    return backup_dir


def apply_update(install_dir: Path):
    payload_dir = install_dir / PAYLOAD_DIR_NAME

    if not payload_dir.exists():
        raise FileNotFoundError(
            f"Could not find {PAYLOAD_DIR_NAME}. "
            f"Put this updater next to the {PAYLOAD_DIR_NAME} folder."
        )

    backup_dir = backup_existing_install(install_dir)

    try:
        for item in payload_dir.iterdir():
            if item.name in PRESERVE_NAMES:
                print(f"Skipping preserved item: {item.name}")
                continue

            destination = install_dir / item.name
            print(f"Updating {item.name}...")
            copy_tree(item, destination)

    except Exception:
        print("\nUpdate failed. Restoring backup...")

        for item in backup_dir.iterdir():
            destination = install_dir / item.name
            copy_tree(item, destination)

        raise

    print("\nUpdate complete.")
    print(f"Backup saved at: {backup_dir}")
    print("You can now launch MTGSiteLocal.exe.")


def main():
    install_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd().resolve()
    apply_update(install_dir)


if __name__ == "__main__":
    main()