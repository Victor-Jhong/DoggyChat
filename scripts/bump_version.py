import re
from datetime import datetime
from pathlib import Path


VERSION_FILE = Path(__file__).resolve().parents[1] / "doggychat" / "version.py"


def bump(version):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError(f"Invalid semantic version: {version}")
    major, minor, patch = (int(part) for part in match.groups())
    patch += 1
    if patch > 9:
        patch = 0
        minor += 1
        if minor > 9:
            minor = 0
            major += 1
    return f"{major}.{minor}.{patch}"


def main():
    source = VERSION_FILE.read_text(encoding="utf-8")
    match = re.search(r'^VERSION = "([^"]+)"$', source, re.MULTILINE)
    if not match:
        raise ValueError(f"Could not find VERSION in {VERSION_FILE}")
    next_version = bump(match.group(1))
    release_time = datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")
    updated = re.sub(
        r'^VERSION = "[^"]+"$',
        f'VERSION = "{next_version}"',
        source,
        count=1,
        flags=re.MULTILINE,
    )
    updated = re.sub(
        r'^RELEASE_TIME = "[^"]+"$',
        f'RELEASE_TIME = "{release_time}"',
        updated,
        count=1,
        flags=re.MULTILINE,
    )
    VERSION_FILE.write_text(updated, encoding="utf-8")
    print(f"DoggyChat {next_version} released {release_time}")


if __name__ == "__main__":
    main()
