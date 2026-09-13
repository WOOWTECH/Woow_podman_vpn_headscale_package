#!/usr/bin/env python3
"""Fail when one of the openclaw host's live secrets appears in a tracked file.

The compose deployment that ran on `woowtechopenclaw` kept three live values outside git: an
ngrok auth token in its `.env`, and Headplane's API key and cookie secret under `runtime/`.
None of them is in this repository, and none of them ever should be - the repo's own comment
said "never commit a populated NGROK_AUTHTOKEN". This is the gate that keeps it that way.

Only the sha256 and the length of each value are recorded here, so the values themselves are
not in the repository. The scan hashes every candidate token of every tracked file, plus every
substring of the recorded length, so a value pasted into the middle of a longer line is still
caught. It never prints a matched value.
"""
import hashlib
import re
import subprocess
import sys

KNOWN = {
    "5096c312a3f6e5360935aa315868af80c6afaa011614fac0846f978311ec8718": 49,
    "30921df91dedce2dc665e9c727659301f4b813169e040f01a02fe82a8841c177": 87,
    "71b3dfc600915a760e2a00b8e7d803633206379aaa8fa25d76f7537d9dbeaef0": 32,
}
TOKEN = re.compile(rb"[^\s=:`'\"<>(),;]{8,128}")


def main() -> int:
    files = [f for f in subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True)
             .stdout.split(b"\0") if f]
    hits = 0
    for name in files:
        try:
            data = open(name, "rb").read()
        except (FileNotFoundError, IsADirectoryError):
            continue
        for token in set(TOKEN.findall(data)):
            for digest, length in KNOWN.items():
                candidates = {token} | {token[i:i + length] for i in range(0, max(1, len(token) - length + 1))}
                if any(hashlib.sha256(c).hexdigest() == digest for c in candidates):
                    print(f"a live host secret was found in {name.decode()}")
                    hits += 1
    print(f"leaked-value scan: {len(files)} tracked files, {hits} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
