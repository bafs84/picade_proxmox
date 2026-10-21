#!/usr/bin/env python3
"""Package only reviewed source files. No credentials, captures or dependencies."""
import argparse
import hashlib
from pathlib import Path
import tarfile


def main():
    p = argparse.ArgumentParser()
    p.add_argument("output")
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    target = Path(args.output).expanduser()
    files = [root / "README.md", root / "VERIFICATION.txt", root / "config.example.json"]
    for directory, pattern in (("vm_arcade", "*.py"), ("scripts", "*.py"),
                               ("tests", "*.py"), ("design", "*.html")):
        files.extend(sorted((root / directory).glob(pattern)))
    def clean(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info
    with tarfile.open(str(target), "w:gz") as archive:
        for source in files:
            archive.add(str(source), arcname="vm-arcade/" + source.relative_to(root).as_posix(),
                        recursive=False, filter=clean)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_name(target.name + ".sha256").write_text(digest + "  " + target.name + "\n")
    print(str(target))
    print("SHA-256 " + digest)


if __name__ == "__main__":
    main()
