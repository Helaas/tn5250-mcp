"""Build src/tn5250_mcp/java/t5250-helper.jar from T5250Helper.java.

The jar holds only our own classes (compiled for Java 11), so any Java 11+
runtime can run the helper, also one without a compiler such as jdk4py's.
jt400 is needed on the class path to compile, but isn't put in the jar.

    python scripts/build_helper.py /path/to/jt400.jar [--javac /path/to/javac]
"""

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

JAVA_DIR = Path(__file__).resolve().parent.parent / "src" / "tn5250_mcp" / "java"
SOURCE = JAVA_DIR / "T5250Helper.java"
JAR = JAVA_DIR / "t5250-helper.jar"


def source_hash() -> str:
    return hashlib.sha256(SOURCE.read_bytes()).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("jt400")
    p.add_argument("--javac", default=shutil.which("javac"))
    a = p.parse_args()
    if not a.javac:
        sys.exit("no javac found; pass --javac /path/to/jdk/bin/javac")
    jar_tool = str(Path(a.javac).with_name("jar"))
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "classes"
        subprocess.run([a.javac, "--release", "11", "-encoding", "UTF-8", "-nowarn",
                        "-cp", a.jt400, "-d", str(out), str(SOURCE)], check=True)
        manifest = Path(tmp) / "MANIFEST.MF"
        manifest.write_text("Main-Class: T5250Helper\nSource-SHA256: %s\n" % source_hash())
        JAR.unlink(missing_ok=True)
        subprocess.run([jar_tool, "--create", "--file", str(JAR), "--manifest", str(manifest),
                        "-C", str(out), "."], check=True)
    print("built %s (%d bytes)" % (JAR, JAR.stat().st_size))


if __name__ == "__main__":
    main()
