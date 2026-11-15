"""Build the extension zip for extensions.blender.org.

    python build_extension.py "path/to/blender.exe"

Copies multitouch_viewport.py to a package (__init__.py) next to blender_manifest.toml,
syncs the manifest version with bl_info, validates and builds the zip into dist/.
"""
import ast
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "multitouch_viewport.py")
MANIFEST = os.path.join(HERE, "blender_manifest.toml")
BUILD = os.path.join(HERE, "build", "multitouch_viewport")
DIST = os.path.join(HERE, "dist")


def bl_info_version():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "bl_info":
            info = ast.literal_eval(node.value)
            return ".".join(str(v) for v in info["version"])
    raise SystemExit("bl_info not found")


def main():
    blender = sys.argv[1] if len(sys.argv) > 1 else "blender"
    version = bl_info_version()
    text = open(MANIFEST, encoding="utf-8").read()
    text = re.sub(r'(?m)^version = ".*"$', 'version = "%s"' % version, text)
    open(MANIFEST, "w", encoding="utf-8", newline="\n").write(text)

    shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(BUILD)
    os.makedirs(DIST, exist_ok=True)
    shutil.copy(SRC, os.path.join(BUILD, "__init__.py"))
    shutil.copy(MANIFEST, BUILD)
    shutil.copy(os.path.join(HERE, "LICENSE"), BUILD)

    subprocess.run([blender, "--factory-startup", "--command", "extension", "validate", BUILD],
                   check=True)
    subprocess.run([blender, "--factory-startup", "--command", "extension", "build",
                    "--source-dir", BUILD, "--output-dir", DIST], check=True)
    print("built version", version, "into", DIST)


if __name__ == "__main__":
    main()
