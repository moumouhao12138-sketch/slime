from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


REPOSITORIES = [
    {
        "name": "CTF-Wiki",
        "path": "/home/kali/knowledges/CTF-Wiki",
        "url": "https://github.com/ctf-wiki/ctf-wiki",
        "license": "CC-BY-NC-SA-4.0",
    },
    {
        "name": "Hello-CTF",
        "path": "/home/kali/knowledges/Hello-CTF",
        "url": "https://github.com/ProbiusOfficial/Hello-CTF",
        "license": "GPL-3.0",
    },
    {
        "name": "CTF-All-In-One",
        "path": "/home/kali/knowledges/CTF-All-In-One",
        "url": "https://github.com/firmianay/CTF-All-In-One",
        "license": "CC-BY-SA-4.0",
    },
    {
        "name": "CTF-Field-Guide",
        "path": "/home/kali/knowledges/CTF-Field-Guide",
        "url": "https://github.com/trailofbits/ctf",
        "license": "CC-BY-SA-4.0",
    },
    {
        "name": "how2heap",
        "path": "/home/kali/knowledges/how2heap",
        "url": "https://github.com/shellphish/how2heap",
        "license": "MIT",
    },
    {
        "name": "MBE",
        "path": "/home/kali/knowledges/MBE",
        "url": "https://github.com/RPISEC/MBE",
        "license": "BSD-2-Clause",
    },
    {
        "name": "crypto-attacks",
        "path": "/home/kali/knowledges/crypto-attacks",
        "url": "https://github.com/jvdsn/crypto-attacks",
        "license": "MIT",
    },
    {
        "name": "ctf-skills",
        "path": "/home/kali/knowledges/ctf-skills",
        "url": "https://github.com/ljagiello/ctf-skills",
        "license": "MIT",
    },
]


def git_value(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def main() -> None:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/slime-cairn/ctf-knowledge.json")
    records = []
    for repository in REPOSITORIES:
        path = Path(repository["path"])
        record = dict(repository)
        record["available"] = path.is_dir()
        record["commit"] = git_value(path, "rev-parse", "HEAD") if path.is_dir() else None
        record["files"] = sum(1 for item in path.rglob("*") if item.is_file()) if path.is_dir() else 0
        records.append(record)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {"schema_version": 1, "repositories": records},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
