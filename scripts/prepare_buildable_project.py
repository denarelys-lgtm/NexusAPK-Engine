#!/usr/bin/env python3
import os
import re
import shutil
import zipfile
from pathlib import Path

SOURCE_DIR = Path("output_project")
TEMPLATES = Path("templates")
OUTPUT_ZIP = Path("rebuild1.zip")
STAGING = Path("_buildable_staging")

LIBRARY_9PATCH_PREFIXES = (
    "abc_", "notification_", "common_", "design_", "mtrl_",
    "tooltip_", "preference_", "m3_", "avd_", "material_", "np_",
)


def fix_cdata(res_dir):
    n = 0
    for p in res_dir.glob("values*/*.xml"):
        try:
            t = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if "]]>" not in t:
            continue
        if "<![CDATA[" not in t:
            new = t.replace("]]>", "")
        else:
            new = re.sub(
                r"\]\]>(</(?:item|string|plurals|array|string-array|integer-array)>)",
                r"\1", t,
            )
        if new != t:
            p.write_text(new, encoding="utf-8")
            n += 1
    return n


def fix_plurals(res_dir):
    n = 0
    for p in res_dir.glob("values*/*.xml"):
        try:
            t = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if "<plurals" not in t:
            continue

        def blk(m):
            def it(im):
                s = im.group(0)
                if re.search(r"\bquantity\s*=", s):
                    return s
                return re.sub(r"<item\b", '<item quantity="other"', s, count=1)
            return re.sub(r"<item\b[^>]*?/?>", it, m.group(0))

        new = re.sub(r"<plurals\b[^>]*>.*?</plurals>", blk, t, flags=re.DOTALL)
        new = re.sub(
            r"<plurals\b(?![^>]*\bname\s*=)[^>]*>.*?</plurals>\s*",
            "", new, flags=re.DOTALL,
        )
        if new != t:
            p.write_text(new, encoding="utf-8")
            n += 1
    return n


def remove_9patch(res_dir):
    n = 0
    for p in res_dir.rglob("*.9.png"):
        if any(p.name.startswith(x) for x in LIBRARY_9PATCH_PREFIXES):
            p.unlink()
            n += 1
    return n


def fix_integers(res_dir):
    n = 0
    for p in res_dir.rglob("integers.xml"):
        try:
            t = p.read_text(encoding="utf-8")
        except Exception:
            continue

        def san(m):
            name = m.group(1)
            raw = m.group(2).strip()
            try:
                if raw.lower().startswith("0x"):
                    int(raw, 16)
                else:
                    int(raw)
                return m.group(0)
            except ValueError:
                return '<integer name="' + name + '">0</integer>'

        new = re.sub(r'<integer\s+name="([^"]+)">\s*([^<]*?)\s*</integer>', san, t)
        if new != t:
            p.write_text(new, encoding="utf-8")
            n += 1
    return n


def fix_hebrew_dir(res_dir):
    """Renombra values-iw a values-he (iw deprecado en AAPT2)."""
    n = 0
    for p in res_dir.glob("values-iw"):
        if p.is_dir():
            target = p.parent / "values-he"
            if not target.exists():
                p.rename(target)
                n += 1
    return n


def apply_fixes(root):
    res = root / "app" / "src" / "main" / "res"
    if not res.is_dir():
        return
    print("[*] CDATA:    " + str(fix_cdata(res)))
    print("[*] PLURALS:  " + str(fix_plurals(res)))
    print("[*] 9-PATCH:  " + str(remove_9patch(res)))
    print("[*] INTEGERS: " + str(fix_integers(res)))
    print("[*] IW->HE:   " + str(fix_hebrew_dir(res)))


def replace_build_files(root):
    pairs = [
        ("settings.gradle", root / "settings.gradle"),
        ("build.gradle", root / "build.gradle"),
        ("app.build.gradle", root / "app" / "build.gradle"),
    ]
    for tpl, dest in pairs:
        src = TEMPLATES / tpl
        if not src.exists():
            continue
        if dest.exists():
            shutil.move(str(dest), str(dest) + ".jadx")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print("    " + str(dest.relative_to(root)))


def inject_wrapper(root):
    src = TEMPLATES / "gradle" / "wrapper"
    dst = root / "gradle" / "wrapper"
    dst.mkdir(parents=True, exist_ok=True)
    for f in ("gradle-wrapper.jar", "gradle-wrapper.properties"):
        if (src / f).exists():
            shutil.copy2(src / f, dst / f)
    for f in ("gradlew", "gradlew.bat"):
        if (TEMPLATES / f).exists():
            shutil.copy2(TEMPLATES / f, root / f)
            if f == "gradlew":
                os.chmod(root / f, 0o755)
    print("[*] Wrapper inyectado")


def sanitize_manifest(root):
    m = root / "app" / "src" / "main" / "AndroidManifest.xml"
    if not m.exists():
        return
    t = m.read_text(encoding="utf-8")
    new = re.sub(r'\s+package="[^"]*"', "", t, count=1)
    if new != t:
        m.write_text(new, encoding="utf-8")
        print("    Manifest saneado")


GITIGNORE = ".gradle/\nbuild/\n!gradle/wrapper/gradle-wrapper.jar\nlocal.properties\n*.apk\n.idea/\n*.iml\n"

README = (
    "# rebuild1\n\n"
    "Proyecto Android autocontenido.\n"
    "Sube este repo a GitHub para compilarlo automaticamente.\n"
)

GRADLE_PROP = (
    "org.gradle.jvmargs=-Xmx4096m\n"
    "org.gradle.parallel=true\n"
    "android.useAndroidX=true\n"
    "android.enableJetifier=true\n"
)


def inject_project_files(root):
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    if (TEMPLATES / "build.yml").exists():
        shutil.copy2(TEMPLATES / "build.yml", wf / "build.yml")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (root / "README.md").write_text(README, encoding="utf-8")
    if not (root / "gradle.properties").exists():
        (root / "gradle.properties").write_text(GRADLE_PROP, encoding="utf-8")
    print("[*] Archivos de proyecto inyectados")


def find_root():
    for p in SOURCE_DIR.rglob("settings.gradle"):
        return p.parent
    for p in SOURCE_DIR.rglob("settings.gradle.kts"):
        return p.parent
    raise RuntimeError("settings.gradle no encontrado")


def stage(root):
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir()
    IGN = shutil.ignore_patterns(
        ".gradle", "build", "*.iml", ".idea",
        "local.properties", "*.apk", "*.jadx",
    )
    for item in root.iterdir():
        dest = STAGING / item.name
        if item.is_dir():
            shutil.copytree(item, dest, ignore=IGN)
        else:
            shutil.copy2(item, dest)
    return STAGING


def zip_it(staging, out):
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(staging):
            dirs[:] = [d for d in dirs if d not in (".git", ".gradle", "build")]
            for f in files:
                full = Path(root) / f
                z.write(full, full.relative_to(staging))
    size = out.stat().st_size / 1024
    print("[*] " + str(out) + ": " + str(round(size, 1)) + " KB")


def main():
    if not SOURCE_DIR.exists():
        raise SystemExit("output_project/ no existe")
    print("=" * 50)
    root = find_root()
    print("[*] Raiz: " + str(root))
    apply_fixes(root)
    replace_build_files(root)
    inject_wrapper(root)
    sanitize_manifest(root)
    inject_project_files(root)
    zip_it(stage(root), OUTPUT_ZIP)
    print("=" * 50)


if __name__ == "__main__":
    main()
