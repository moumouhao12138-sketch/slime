from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys


DEFINITIONS = [
    ("nmap", "network", False, ["NET_RAW"]),
    ("masscan", "network", False, ["NET_RAW"]),
    ("curl", "http", False, []),
    ("whatweb", "web", False, []),
    ("ffuf", "web", False, []),
    ("feroxbuster", "web", False, []),
    ("gobuster", "web", False, []),
    ("dirsearch", "web", False, []),
    ("nikto", "web", False, []),
    ("nuclei", "web", False, []),
    ("sqlmap", "web", True, []),
    ("wfuzz", "web", False, []),
    ("wafw00f", "web", False, []),
    ("sslscan", "web", False, []),
    ("searchsploit", "knowledge", False, []),
    ("gdb", "pwn", True, []),
    ("checksec", "pwn", False, []),
    ("radare2", "reverse", True, []),
    ("strace", "debug", False, []),
    ("ltrace", "debug", False, []),
    ("binwalk", "forensics", False, []),
    ("exiftool", "forensics", False, []),
    ("tshark", "forensics", False, ["NET_RAW"]),
    ("tcpdump", "network", False, ["NET_RAW"]),
    ("naabu", "network", False, ["NET_RAW"]),
    ("ncat", "network", False, []),
    ("chisel", "network", False, []),
    ("john", "password", False, []),
    ("hashcat", "password", False, []),
    ("hydra", "password", False, []),
    ("python3", "scripting", False, []),
    ("tmux", "session", True, []),
    ("katana", "web", False, []),
    ("dalfox", "web", False, []),
    ("bloodyAD", "active-directory", False, []),
    ("coercer", "active-directory", False, []),
    ("enum4linux-ng", "active-directory", False, []),
    ("netexec", "active-directory", False, []),
    ("kerbrute", "active-directory", False, []),
    ("cloudfox", "cloud", False, []),
    ("gitleaks", "secrets", False, []),
    ("adb", "mobile", False, []),
    ("playwright-cli", "browser", True, []),
    ("codex", "agent", True, []),
    ("claude", "agent", True, []),
    ("pi", "agent", True, []),
]


def version_of(path: str) -> str:
    for args in ([path, "--version"], [path, "-V"]):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
            line = (result.stdout or result.stderr).splitlines()
            if line:
                return line[0][:200]
        except Exception:
            pass
    return "unknown"


def main() -> None:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/slime-cairn/tools.json")
    records = []
    for name, category, interactive, capabilities in DEFINITIONS:
        path = shutil.which(name)
        records.append(
            {
                "name": name,
                "path": path,
                "version": version_of(path) if path else "unavailable",
                "category": category,
                "interactive": interactive,
                "required_capabilities": capabilities,
                "available": path is not None,
            }
        )
    output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
