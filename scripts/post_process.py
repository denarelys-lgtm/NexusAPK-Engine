#!/usr/bin/env python3
import os
import re
import sys

ROOT = "output_project"


def clean_file(content):
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if content.startswith("\ufeff"):
        content = content[1:]
    content = re.sub(r'/\* Dump dbs parameters.*?\*/', '', content, flags=re.DOTALL)
    content = re.sub(r'[ \t]+\n', '\n', content)
    return content


def main():
    if not os.path.isdir(ROOT):
        print("ERROR: " + ROOT + " no existe")
        sys.exit(1)

    print("[*] Saneando Java...")
    total = 0
    modified = 0
    bad = 0

    for sub, _, files in os.walk(ROOT):
        for f in files:
            if not f.endswith(".java"):
                continue
            total += 1
            p = os.path.join(sub, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    orig = fh.read()
            except Exception:
                continue
            new = clean_file(orig)
            bad += len(re.findall(
                r'UnsupportedOperationException\("Method not decompiled', new
            ))
            if new != orig:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(new)
                modified += 1

    print("[*] Archivos Java: " + str(total) + ", modificados: " + str(modified))
    print("[!] Metodos sin decompilar: " + str(bad))


if __name__ == "__main__":
    main()
