"""Persistent, machine-local ntfy subscription with one-time naming."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import warnings


def config_path():
    base = Path(os.environ.get("PROGRAMDATA", str(Path.home() / ".config")))
    return base / "SpectralSweep" / "notifications.json"


def _short_url(name, seed=None):
    # Keep manual entry short and avoid visually ambiguous characters.
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:16].rstrip("-") or "pc"
    suffix = ("".join(alphabet[value % len(alphabet)] for value in hashlib.sha256(seed.encode()).digest()[:6])
              if seed else "".join(secrets.choice(alphabet) for _ in range(6)))
    topic = f"ss-{slug}-{suffix}"
    return f"https://ntfy.sh/{topic}"


def configure(name, path=None):
    """Create a subscription once; never overwrite an existing identity."""
    path = Path(path) if path is not None else config_path()
    name = name.strip()
    if not name:
        raise ValueError("PC name cannot be empty")
    url = _short_url(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish a complete file atomically, without replacing another process's setup.
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"name": name, "url": url}, stream, indent=2)
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return url


def is_short_url(url):
    return bool(re.fullmatch(r"https://ntfy\.sh/ss-[a-z0-9-]{1,16}-[abcdefghjkmnpqrstuvwxyz23456789]{6}", url))


def shorten_url(path=None):
    """Explicit one-time migration; preserve the locked name and original URL."""
    path = Path(path) if path is not None else config_path()
    data = read_config(path)
    if data is None:
        raise ValueError("Set up this PC first")
    if is_short_url(data["url"]):
        return data["url"]
    data["previous_url"] = data["url"]
    data["url"] = _short_url(data["name"], seed=data["previous_url"])
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return data["url"]


def read_config(path=None):
    path = Path(path) if path is not None else config_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(url, str) or not re.fullmatch(r"https://ntfy\.sh/[a-z0-9-]+", url):
        raise ValueError(f"Invalid ntfy configuration: {path}; restore the saved configuration")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError(f"Invalid PC name in {path}; restore the saved configuration")
    return data


def get_ntfy_url(path=None):
    data = read_config(path)
    return data["url"] if data else None


def runtime_url(path=None):
    """Keep optional notifications from preventing instrument-app startup."""
    try:
        return get_ntfy_url(path)
    except (OSError, ValueError) as exc:
        warnings.warn(f"ntfy notifications disabled: {exc}", RuntimeWarning, stacklevel=2)
        return None


def main():
    parser = argparse.ArgumentParser(description="Set up or display this PC's permanent ntfy subscription")
    parser.add_argument("--name", help="Set the PC name once (also available in Settings)")
    args = parser.parse_args()
    try:
        url = configure(args.name) if args.name is not None else get_ntfy_url()
    except FileExistsError:
        parser.exit(1, f"This PC is already configured. Its subscription cannot be renamed.\n{get_ntfy_url()}\n")
    print(url or "Not configured. Open Settings > PC Notifications to set up this PC.")
    print(f"Configuration: {config_path()}")


if __name__ == "__main__":
    main()
