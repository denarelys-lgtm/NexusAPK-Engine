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
    "abc_",
    "notification_",
    "common_",
    "design_",
    "mtrl_",
    "tooltip_",
    "preference_",
    "m3_",
    "avd_",
    "material_",
    "np_",
)


def fix_cdata(res_dir):
    """
    Corrige valores XML que contienen un cierre CDATA inválido (]]>).
    """

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
                r"\1",
                t,
            )

        if new != t:
            p.write_text(new, encoding="utf-8")
            n += 1

    return n


def fix_plurals(res_dir):
    """
    Sanitiza recursos <plurals> para evitar fallos internos de AAPT2.

    Algunos APK decompilados contienen plurals que JADX/Apktool reconstruye
    con atributos o nodos que AAPT2 no puede interpretar. En particular,
    TableExtractor.parsePlural() puede lanzar NullPointerException cuando un
    <item> no tiene el atributo quantity esperado.

    Primero intentamos reconstruir los plurals válidos. Si un bloque no puede
    reconstruirse de forma segura, se elimina completo para que el proyecto
    siga siendo compilable.
    """

    n = 0
    valid_quantities = {"zero", "one", "two", "few", "many", "other"}

    for p in res_dir.glob("values*/*.xml"):
        try:
            t = p.read_text(encoding="utf-8")
        except Exception:
            continue

        if "<plurals" not in t.lower():
            continue

        changed = False

        def rebuild_plural(match):
            nonlocal changed

            opening = match.group(1)
            body = match.group(2)

            name_match = re.search(
                r'\bname\s*=\s*(["\'])(.*?)\1',
                opening,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not name_match or not name_match.group(2).strip():
                changed = True
                return ""

            name = name_match.group(2).strip()
            items = []

            # Extrae SOLO elementos item directos del bloque.
            for im in re.finditer(
                r'<item\b([^>]*)>(.*?)</item\s*>',
                body,
                flags=re.IGNORECASE | re.DOTALL,
            ):
                attrs = im.group(1)
                value = im.group(2)

                qm = re.search(
                    r'\bquantity\s*=\s*(["\'])(.*?)\1',
                    attrs,
                    flags=re.IGNORECASE | re.DOTALL,
                )

                if not qm:
                    # Un item sin quantity es precisamente una de las
                    # estructuras que provoca el NPE de AAPT2.
                    changed = True
                    continue

                quantity = qm.group(2).strip()
                if quantity not in valid_quantities:
                    changed = True
                    continue

                # Conserva el texto, pero elimina cualquier XML interno
                # problemático (xliff/span/etc.) que pueda volver a romper
                # el TableExtractor.
                value = re.sub(r'<[^>]+>', '', value)
                value = value.strip()
                items.append((quantity, value))

            # También detecta items autocerrados, pero solo si tienen
            # quantity explícito.
            for im in re.finditer(
                r'<item\b([^>]*)/\s*>',
                body,
                flags=re.IGNORECASE | re.DOTALL,
            ):
                attrs = im.group(1)
                qm = re.search(
                    r'\bquantity\s*=\s*(["\'])(.*?)\1',
                    attrs,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                if not qm:
                    changed = True
                    continue

                quantity = qm.group(2).strip()
                if quantity not in valid_quantities:
                    changed = True
                    continue

                items.append((quantity, ""))

            # Sin items válidos: eliminar el plural completo.
            if not items:
                changed = True
                return ""

            # AAPT2 no acepta cantidades duplicadas. Conservamos el primero.
            unique = []
            seen = set()
            for quantity, value in items:
                if quantity in seen:
                    changed = True
                    continue
                seen.add(quantity)
                unique.append((quantity, value))

            result = ['<plurals name="' + name + '">']
            for quantity, value in unique:
                result.append(
                    '    <item quantity="' + quantity + '">'
                    + value
                    + '</item>'
                )
            result.append('</plurals>')

            rebuilt = "\n".join(result)
            if rebuilt != match.group(0):
                changed = True
            return rebuilt

        # Procesa bloques plurals completos.
        new = re.sub(
            r'<plurals\b([^>]*)>(.*?)</plurals\s*>',
            rebuild_plural,
            t,
            flags=re.IGNORECASE | re.DOTALL,
        )

        # Si queda algún <plurals> mal cerrado, elimínalo para impedir que
        # llegue a AAPT2 un bloque incompleto.
        if re.search(r'<plurals\b', new, flags=re.IGNORECASE):
            open_count = len(re.findall(r'<plurals\b', new, flags=re.IGNORECASE))
            close_count = len(re.findall(r'</plurals\s*>', new, flags=re.IGNORECASE))
            if open_count != close_count:
                new = re.sub(
                    r'<plurals\b[^>]*>.*?(?=</resources\s*>)',
                    '',
                    new,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                changed = True

        if changed and new != t:
            p.write_text(new, encoding="utf-8")
            n += 1

    return n


def remove_9patch(res_dir):
    """
    Convierte los Nine-Patch problemáticos en PNG normales.

    Los proyectos decompilados pueden contener archivos *.9.png que
    AAPT2 no puede reconstruir porque sus marcas Nine-Patch son inválidas.
    Para mantener el recurso disponible y evitar que AAPT2 intente
    interpretarlo como Nine-Patch, se elimina únicamente la extensión
    ".9" del nombre.

    Si ya existe un PNG con el mismo nombre, se elimina el Nine-Patch
    problemático para evitar dos recursos con el mismo identificador.
    """

    n = 0

    for p in res_dir.rglob("*.9.png"):
        normal = p.with_name(p.name[:-7] + ".png")

        try:
            if normal.exists():
                p.unlink()
                n += 1
                print(
                    "[9-PATCH] Eliminado por conflicto: "
                    + str(p.relative_to(res_dir))
                )
                continue

            p.rename(normal)
            n += 1
            print(
                "[9-PATCH] Convertido a PNG normal: "
                + str(normal.relative_to(res_dir))
            )

        except Exception as e:
            print(
                "[!] No se pudo procesar 9-patch "
                + str(p)
                + ": "
                + str(e)
            )

    return n


def fix_integers(res_dir):
    """
    Corrige recursos integers.xml con valores que no son enteros válidos.
    """

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

        new = re.sub(
            r'<integer\s+name="([^"]+)">\s*([^<]*?)\s*</integer>',
            san,
            t,
        )

        if new != t:
            p.write_text(new, encoding="utf-8")
            n += 1

    return n


def fix_hebrew_dir(res_dir):
    """
    Renombra values-iw a values-he.

    'iw' es el código antiguo para hebreo y AAPT2 puede
    presentar problemas con esa carpeta en determinados proyectos.
    """

    n = 0

    for p in res_dir.glob("values-iw"):
        if p.is_dir():
            target = p.parent / "values-he"

            if not target.exists():
                p.rename(target)
                n += 1

    return n


def apply_fixes(root):
    """
    Ejecuta todas las correcciones sobre los recursos del proyecto.
    """

    res = root / "app" / "src" / "main" / "res"

    if not res.is_dir():
        print("[!] No existe la carpeta de recursos:")
        print("    " + str(res))
        return

    print("[*] CDATA:    " + str(fix_cdata(res)))
    print("[*] PLURALS:  " + str(fix_plurals(res)))
    print("[*] 9-PATCH:  " + str(remove_9patch(res)))
    print("[*] INTEGERS: " + str(fix_integers(res)))
    print("[*] IW->HE:   " + str(fix_hebrew_dir(res)))


def replace_build_files(root):
    """
    Reemplaza los archivos Gradle principales por las plantillas
    preparadas para el proyecto reconstruido.
    """

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
            shutil.move(
                str(dest),
                str(dest) + ".jadx",
            )

        dest.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.copy2(
            src,
            dest,
        )

        print(
            "    "
            + str(dest.relative_to(root))
        )


def inject_wrapper(root):
    """
    Inyecta el Gradle Wrapper preparado por el proyecto.
    """

    src = TEMPLATES / "gradle" / "wrapper"
    dst = root / "gradle" / "wrapper"

    dst.mkdir(
        parents=True,
        exist_ok=True,
    )

    for f in (
        "gradle-wrapper.jar",
        "gradle-wrapper.properties",
    ):
        if (src / f).exists():
            shutil.copy2(
                src / f,
                dst / f,
            )

    for f in (
        "gradlew",
        "gradlew.bat",
    ):
        if (TEMPLATES / f).exists():
            shutil.copy2(
                TEMPLATES / f,
                root / f,
            )

            if f == "gradlew":
                os.chmod(
                    root / f,
                    0o755,
                )

    print("[*] Wrapper inyectado")


def sanitize_manifest(root):
    """
    Elimina el atributo package antiguo del AndroidManifest.
    """

    m = (
        root
        / "app"
        / "src"
        / "main"
        / "AndroidManifest.xml"
    )

    if not m.exists():
        return

    t = m.read_text(
        encoding="utf-8"
    )

    new = re.sub(
        r'\s+package="[^"]*"',
        "",
        t,
        count=1,
    )

    if new != t:
        m.write_text(
            new,
            encoding="utf-8",
        )

        print(
            "    Manifest saneado"
        )


GITIGNORE = (
    ".gradle/\n"
    "build/\n"
    "!gradle/wrapper/gradle-wrapper.jar\n"
    "local.properties\n"
    "*.apk\n"
    ".idea/\n"
    "*.iml\n"
)


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
    """
    Inyecta archivos auxiliares del proyecto.
    """

    wf = (
        root
        / ".github"
        / "workflows"
    )

    wf.mkdir(
        parents=True,
        exist_ok=True,
    )

    if (TEMPLATES / "build.yml").exists():
        shutil.copy2(
            TEMPLATES / "build.yml",
            wf / "build.yml",
        )

    (root / ".gitignore").write_text(
        GITIGNORE,
        encoding="utf-8",
    )

    (root / "README.md").write_text(
        README,
        encoding="utf-8",
    )

    if not (root / "gradle.properties").exists():
        (root / "gradle.properties").write_text(
            GRADLE_PROP,
            encoding="utf-8",
        )

    print(
        "[*] Archivos de proyecto inyectados"
    )


def find_root():
    """
    Busca la raíz del proyecto Android generado.
    """

    for p in SOURCE_DIR.rglob("settings.gradle"):
        return p.parent

    for p in SOURCE_DIR.rglob("settings.gradle.kts"):
        return p.parent

    raise RuntimeError(
        "settings.gradle no encontrado"
    )


def stage(root):
    """
    Copia el proyecto a una carpeta temporal para generar el ZIP.
    """

    if STAGING.exists():
        shutil.rmtree(STAGING)

    STAGING.mkdir()

    IGN = shutil.ignore_patterns(
        ".gradle",
        "build",
        "*.iml",
        ".idea",
        "local.properties",
        "*.apk",
        "*.jadx",
    )

    for item in root.iterdir():
        dest = STAGING / item.name

        if item.is_dir():
            shutil.copytree(
                item,
                dest,
                ignore=IGN,
            )

        else:
            shutil.copy2(
                item,
                dest,
            )

    return STAGING


def zip_it(staging, out):
    """
    Genera el ZIP final del proyecto.
    """

    if out.exists():
        out.unlink()

    with zipfile.ZipFile(
        out,
        "w",
        zipfile.ZIP_DEFLATED,
    ) as z:

        for root, dirs, files in os.walk(staging):

            dirs[:] = [
                d
                for d in dirs
                if d not in (
                    ".git",
                    ".gradle",
                    "build",
                )
            ]

            for f in files:
                full = Path(root) / f

                z.write(
                    full,
                    full.relative_to(staging),
                )

    size = out.stat().st_size / 1024

    print(
        "[*] "
        + str(out)
        + ": "
        + str(round(size, 1))
        + " KB"
    )


def main():
    """
    Punto de entrada principal.
    """

    if not SOURCE_DIR.exists():
        raise SystemExit(
            "output_project/ no existe"
        )

    print("=" * 50)

    root = find_root()

    print(
        "[*] Raiz: "
        + str(root)
    )

    # Primero corregimos los recursos.
    apply_fixes(root)

    # Después colocamos los Gradle preparados.
    replace_build_files(root)

    # Gradle Wrapper.
    inject_wrapper(root)

    # AndroidManifest.
    sanitize_manifest(root)

    # Archivos auxiliares.
    inject_project_files(root)

    # Crear ZIP final.
    zip_it(
        stage(root),
        OUTPUT_ZIP,
    )

    print("=" * 50)
    print("[*] PROCESO TERMINADO")


if __name__ == "__main__":
    main()
