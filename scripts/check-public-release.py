#!/usr/bin/env python3
"""Check only the exact public-source allowlist. Never read runtime profiles."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

PATTERNS = (
    ("literal personal home path", re.compile(r"(?:/Users|/home)/[A-Za-z0-9_.-]+/")),
    ("private identifier format", re.compile(r"\b01a[0-9a-f]{5}-[0-9a-f-]{27,}\b",re.I)),
    ("provider credential", re.compile(r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("credential in URL", re.compile(r"https?://[^\s/@:]+:[^\s/@]+@")),
)
FORBIDDEN_EXTENSIONS = {".png",".jpg",".jpeg",".gif",".icns",".sqlite",".db",".jsonl",".log",".zip",".dmg",".p12",".pem"}


def command(root, *args):
    return subprocess.run(["git","-C",str(root),*args],capture_output=True,text=True)


def check(root:Path, check_git=False, archive:Path|None=None):
    root=root.resolve()
    manifest=root/"PUBLIC_FILES.txt"
    if not manifest.is_file() or manifest.is_symlink(): raise ValueError("A regular PUBLIC_FILES.txt is required")
    names=[line.strip() for line in manifest.read_text().splitlines() if line.strip() and not line.startswith("#")]
    if len(names) != len(set(names)) or not names: raise ValueError("The public allowlist is empty or repeated")
    findings=[];hashes={}
    for name in names:
        relative=Path(name)
        if relative.is_absolute() or ".." in relative.parts: raise ValueError("Unsafe allowlist path")
        path=root/relative
        if path.is_symlink() or any(part.is_symlink() for part in path.parents if part != root.parent): raise ValueError("Redirected allowlist path")
        if not path.is_file() or path.stat().st_size > 2*1024*1024: raise ValueError("Missing or unexpectedly large allowlisted source")
        if path.suffix.lower() in FORBIDDEN_EXTENSIONS: raise ValueError("Runtime or binary artifact is not public source")
        data=path.read_bytes()
        try: source=data.decode("utf-8")
        except UnicodeDecodeError as error: raise ValueError("A public file is not UTF-8 source") from error
        if b"\0" in data: raise ValueError("A public source file contains binary data")
        hashes[name]=hashlib.sha256(data).hexdigest()
        for label,pattern in PATTERNS:
            for match in pattern.finditer(source):
                findings.append({"file":name,"line":source.count("\n",0,match.start())+1,"kind":label})
    if check_git:
        tracked=command(root,"ls-files","-z")
        if tracked.returncode: raise ValueError("Cannot inspect Git files")
        actual={name for name in tracked.stdout.split("\0") if name}
        if actual != set(names): raise ValueError("Tracked files differ from the exact public allowlist")
        # Inspect all committed public snapshots, so deleting a secret from HEAD
        # cannot silently leave it in a published historical commit.
        commits=command(root,"rev-list","--all")
        if commits.returncode: raise ValueError("Cannot inspect commit history")
        for commit in commits.stdout.splitlines():
            authored=command(root,"show","-s","--format=%an|%ae|%cn|%ce",commit)
            expected="Chat Transfer Contributors|contributors@users.noreply.github.com|Chat Transfer Contributors|contributors@users.noreply.github.com"
            if authored.stdout.strip() != expected: raise ValueError("Git author metadata requires explicit privacy review")
            tree=command(root,"ls-tree","-r","--name-only",commit)
            if set(tree.stdout.splitlines()) != set(names): raise ValueError("Historical tree differs from the public allowlist")
            for name in names:
                blob=command(root,"show",f"{commit}:{name}")
                if blob.returncode: raise ValueError("Cannot read a committed public source blob")
                for label,pattern in PATTERNS:
                    if pattern.search(blob.stdout): findings.append({"file":name,"commit":commit[:12],"kind":label})
    if archive:
        if findings: raise ValueError("Refusing to package flagged sources")
        if archive.exists(): raise ValueError("Refusing to replace an existing archive")
        with zipfile.ZipFile(archive,"x",compression=zipfile.ZIP_DEFLATED) as output:
            for name in names: output.write(root/name,"chat-transfer/"+name)
        with zipfile.ZipFile(archive) as source:
            if set(source.namelist()) != {"chat-transfer/"+name for name in names}: raise ValueError("Archive differs from the public allowlist")
            for name in names:
                if hashlib.sha256(source.read("chat-transfer/"+name)).hexdigest() != hashes[name]: raise ValueError("Archive source hash mismatch")
    return {"ok":not findings,"files_checked":len(names),"findings":findings,"sha256":hashes,"git_checked":check_git,"archive_checked":archive is not None}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument("--git",action="store_true")
    parser.add_argument("--archive",type=Path)
    args=parser.parse_args()
    try:
        result=check(args.root,args.git,args.archive)
        print(json.dumps(result,indent=2));return 0 if result["ok"] else 1
    except (ValueError,OSError,zipfile.BadZipFile) as error:
        print(json.dumps({"ok":False,"message":str(error)}));return 1
if __name__ == "__main__": raise SystemExit(main())
