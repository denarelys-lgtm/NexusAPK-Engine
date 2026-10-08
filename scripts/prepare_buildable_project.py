#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NexusAPK-Engine — scripts/prepare_buildable_project.py

Prepara un proyecto Android decompilado (JADX + Apktool) para recompilarlo
con Gradle 8.13 + AGP + AAPT2 sobre Java 17 / SDK 34.

Reparaciones aplicadas:
  * remove_9patch   : renombra .9.png inválidos a .png
  * fix_plurals     : normaliza <plurals> para que AAPT2 no reviente con
                      TableExtractor.parsePlural() NullPointerException
  * fix_cdata       : convierte CDATA problemática en texto escapado
  * fix_integers    : normaliza <integer> mal tipados
  * fix_hebrew      : ajustes de RTL en values-iw / values-he
  * fix_manifest    : normaliza AndroidManifest.xml
  * replace_gradle_files / replace_wrapper : plantillas propias

Uso:
    python3 scripts/prepare_buildable_project.py <project_dir>
"""

from __future__ import annotations

import os
import re
import sys
import shutil
import logging
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
import xml.etree.ElementTree as ET

# --------------------------------------------------------------------------- #
# Logging / configuración
# --------------------------------------------------------------------------- #

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("nexus")

RES_REL      = Path("app/src/main/res")
MANIFEST_REL = Path("app/src/main/AndroidManifest.xml")
APP_GRADLE   = Path("app/build.gradle")
ROOT_GRADLE  = Path("build.gradle")
SETTINGS     = Path("settings.gradle")
GRADLE_PROPS = Path("gradle.properties")
WRAPPER_DIR  = Path("gradle/wrapper")
WRAPPER_PROPS = WRAPPER_DIR / "gradle-wrapper.properties"

VALID_QUANTITIES     = ("zero", "one", "two", "few", "many", "other")
VALID_QUANTITIES_SET = set(VALID_QUANTITIES)

# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def write_text(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8")


def iter_values_xml(res_root: Path):
    """Itera TODOS los values*/ *.xml bajo res/."""
    if not res_root.is_dir():
        return
    for sub in sorted(res_root.iterdir()):
        if not sub.is_dir():
            continue
        if not sub.name.startswith("values"):
            continue
        for f in sorted(sub.glob("*.xml")):
            yield f


# --------------------------------------------------------------------------- #
# 1. 9-patch
# --------------------------------------------------------------------------- #

def remove_9patch(res_root: Path) -> None:
    """Renombra .9.png que no son 9-patch reales (sin chunk 'npTc') a .png."""
    if not res_root.is_dir():
        return
    renamed = 0
    for p in res_root.rglob("*.9.png"):
        try:
            data = p.read_bytes()
        except Exception as e:
            log.warning(f"remove_9patch: no se pudo leer {p}: {e}")
            continue
        if b"npTc" in data:
            # Es un 9-patch válido, no tocar
            continue
        new_name = p.name[:-len(".9.png")] + ".png"
        new_path = p.with_name(new_name)
        try:
            p.rename(new_path)
            renamed += 1
        except Exception as e:
            log.warning(f"remove_9patch: no se pudo renombrar {p}: {e}")
    if renamed:
        log.info(f"remove_9patch: {renamed} recursos convertidos .9.png -> .png")


# --------------------------------------------------------------------------- #
# 2. Plurals  (CORAZÓN DEL FIX)
# --------------------------------------------------------------------------- #

_PLURALS_RE   = re.compile(r"<plurals\b([^>]*)>(.*?)</plurals>", re.DOTALL | re.IGNORECASE)
_QUANTITY_RE  = re.compile(r'quantity\s*=\s*"([^"]*)"', re.IGNORECASE)
_NAME_RE      = re.compile(r'name\s*=\s*"([^"]*)"', re.IGNORECASE)
_BARE_AMP_RE  = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")
_CDATA_INNER  = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_COMMENT_RE   = re.compile(r"<!--.*?-->", re.DOTALL)


def _find_items(body: str):
    """
    Devuelve [(attrs_raw, content), ...] para cada <item> dentro de `body`.

    - Soporta <item .../> y <item ...>contenido</item>
    - Soporta atributos con cualquier espaciado / saltos de línea
    - No asume orden ni estructura
    """
    items = []
    pos = 0
    while True:
        m = re.search(r"<item\b", body[pos:], re.IGNORECASE)
        if not m:
            break
        item_start = pos + m.start()
        open_end = body.find(">", item_start)
        if open_end < 0:
            break
        raw_open = body[item_start:open_end]
        self_close = raw_open.rstrip().endswith("/")
        attrs = raw_open[len("<item"):]
        if self_close:
            attrs = attrs.rstrip()[:-1]  # quitar la '/'
            items.append((attrs, ""))
            pos = open_end + 1
            continue
        content_start = open_end + 1
        end_m = re.search(r"</item\s*>", body[content_start:], re.IGNORECASE)
        if not end_m:
            # <item> sin cierre → tomar el resto
            items.append((attrs, body[content_start:]))
            break
        content = body[content_start: content_start + end_m.start()]
        items.append((attrs, content))
        pos = content_start + end_m.end()
    return items


def _sanitize_item_content(s: str) -> str:
    """Convierte el contenido crudo de un <item> en texto XML válido."""
    s = s.strip()

    # CDATA → texto escapado (una sola vez, sin doble escape)
    m = re.fullmatch(r"<!\[CDATA\[(.*?)\]\]>", s, re.DOTALL)
    if m:
        return xml_escape(m.group(1)).strip()

    # Comentarios internos fuera
    s = _COMMENT_RE.sub("", s)

    # & "sueltos" → &amp;, pero preservando entidades ya válidas
    s = _BARE_AMP_RE.sub("&amp;", s)

    return s.strip()


def _rebuild_plurals(attrs: str, body: str, source: Path):
    """
    Reconstruye un bloque <plurals ...> ... </plurals>.
    Devuelve (nuevo_bloque, num_reparaciones).
    """
    name_m = _NAME_RE.search(attrs)
    name = name_m.group(1) if name_m else "<?>"

    raw_items = _find_items(body)

    if not raw_items:
        # <plurals> sin items: AAPT2 podría NPE. No lo tocamos aquí, se
        # reportará en auditoría; preferimos no generar contenido artificial.
        log.warning(
            f"{source}: <plurals name=\"{name}\"> sin <item>. "
            f"Se deja intacto (auditoría lo reportará)."
        )
        return f"<plurals{attrs}>{body}</plurals>", 0

    seen = {}     # quantity -> contenido saneado
    order = []    # orden de aparición de cantidades
    repaired = 0

    for item_attrs, content in raw_items:
        q_m = _QUANTITY_RE.search(item_attrs)
        q = q_m.group(1).strip() if q_m else ""

        if q not in VALID_QUANTITIES_SET:
            if not q:
                if "other" not in seen:
                    q = "other"
                    repaired += 1
                    log.warning(
                        f"{source}: <plurals name=\"{name}\"> <item> sin "
                        f"quantity → asignado 'other'"
                    )
                else:
                    repaired += 1
                    log.warning(
                        f"{source}: <plurals name=\"{name}\"> <item> sin "
                        f"quantity y 'other' ya existe → descartado"
                    )
                    continue
            else:
                repaired += 1
                log.warning(
                    f"{source}: <plurals name=\"{name}\"> quantity "
                    f"inválida '{q}' → descartado"
                )
                continue

        if q in seen:
            repaired += 1
            log.warning(
                f"{source}: <plurals name=\"{name}\"> quantity "
                f"duplicada '{q}' → descartado"
            )
            continue

        sanitized = _sanitize_item_content(content)
        if sanitized != content:
            repaired += 1

        seen[q] = sanitized
        order.append(q)

    # Si NO quedó ningún item, rescatamos el primero (nunca borrar el recurso)
    if not order:
        _, content0 = raw_items[0]
        seen["other"] = _sanitize_item_content(content0)
        order = ["other"]
        repaired += 1
        log.warning(
            f"{source}: <plurals name=\"{name}\"> ningún item válido → "
            f"se conserva el primero como quantity=\"other\""
        )

    # Si no hubo cambios reales, devolvemos el bloque original intacto
    if repaired == 0:
        return f"<plurals{attrs}>{body}</plurals>", 0

    lines = [f"<plurals{attrs}>"]
    for q in order:
        lines.append(f'    <item quantity="{q}">{seen[q]}</item>')
    lines.append("</plurals>")
    return "\n".join(lines), repaired


def fix_plurals(res_root: Path) -> None:
    """
    Recorre TODOS los values*/**.xml y repara cada <plurals>.
    Verifica well-formedness antes de escribir.
    """
    if not res_root.is_dir():
        log.warning(f"fix_plurals: {res_root} no existe")
        return

    total_files = 0
    total_blocks = 0

    for f in iter_values_xml(res_root):
        try:
            text = read_text(f)
        except Exception as e:
            log.warning(f"fix_plurals: no se pudo leer {f}: {e}")
            continue

        if "<plurals" not in text:
            continue

        out = []
        pos = 0
        blocks = 0
        for m in _PLURALS_RE.finditer(text):
            out.append(text[pos:m.start()])
            pos = m.end()
            rebuilt, n = _rebuild_plurals(m.group(1), m.group(2), f)
            blocks += n
            out.append(rebuilt)
        out.append(text[pos:])
        new_text = "".join(out)

        if new_text == text:
            continue

        # Verificación: solo escribimos si sigue siendo XML bien formado
        try:
            ET.fromstring(new_text)
        except ET.ParseError as e:
            log.error(
                f"fix_plurals: {f} quedaría XML inválido ({e}); "
                f"se omite la escritura para no empeorar el recurso"
            )
            continue

        write_text(f, new_text)
        total_files += 1
        total_blocks += blocks
        log.info(f"fix_plurals: {f} reparado ({blocks} bloques)")

    if total_files:
        log.info(
            f"fix_plurals: {total_files} archivos / {total_blocks} "
            f"bloques reparados en total"
        )
    else:
        log.info("fix_plurals: no se encontraron bloques <plurals> a reparar")


def audit_plurals(res_root: Path) -> int:
    """
    Pase final de diagnóstico: reporta cualquier <item> dentro de <plurals>
    que se haya quedado sin 'quantity' válida. Devuelve el nº de incidencias.
    """
    issues = 0
    for f in iter_values_xml(res_root):
        try:
            text = read_text(f)
        except Exception:
            continue
        if "<plurals" not in text:
            continue
        for m in _PLURALS_RE.finditer(text):
            for attrs, _ in _find_items(m.group(2)):
                q_m = _QUANTITY_RE.search(attrs)
                q = q_m.group(1).strip() if q_m else ""
                if q not in VALID_QUANTITIES_SET:
                    issues += 1
                    log.error(
                        f"audit_plurals: {f}: <item> sin quantity válida "
                        f"(encontrado: {q!r})"
                    )
    if issues == 0:
        log.info("audit_plurals: OK — todos los <item> tienen quantity válida")
    return issues


# --------------------------------------------------------------------------- #
# 3. CDATA
# --------------------------------------------------------------------------- #

def fix_cdata(res_root: Path) -> None:
    """Convierte bloques CDATA (fuera de <plurals>) a texto XML escapado."""
    if not res_root.is_dir():
        return
    touched = 0
    for f in iter_values_xml(res_root):
        try:
            text = read_text(f)
        except Exception:
            continue
        if "<![CDATA[" not in text:
            continue

        def _repl(m):
            return xml_escape(m.group(1))

        new_text = _CDATA_INNER.sub(_repl, text)

        if new_text == text:
            continue
        try:
            ET.fromstring(new_text)
        except ET.ParseError as e:
            log.warning(f"fix_cdata: {f} quedaría inválido ({e}); se omite")
            continue
        write_text(f, new_text)
        touched += 1
    if touched:
        log.info(f"fix_cdata: {touched} archivos actualizados")


# --------------------------------------------------------------------------- #
# 4. Integers
# --------------------------------------------------------------------------- #

def fix_integers(res_root: Path) -> None:
    """
    Normaliza <integer> mal tipados. Si el APK original tenía <integer>
    con contenido no numérico, AAPT2 rechaza. Los sustituimos por 0 y
    logueamos para que sea visible.
    """
    if not res_root.is_dir():
        return
    pattern = re.compile(r"<integer\b([^>]*)>(.*?)</integer>", re.DOTALL | re.IGNORECASE)
    touched = 0
    for f in iter_values_xml(res_root):
        try:
            text = read_text(f)
        except Exception:
            continue
        if "<integer" not in text:
            continue

        def _repl(m):
            attrs, body = m.group(1), m.group(2).strip()
            if re.fullmatch(r"-?\d+", body or ""):
                return m.group(0)
            log.warning(
                f"fix_integers: {f}: <integer{attrs}> con valor no numérico "
                f"{body!r} → 0"
            )
            return f"<integer{attrs}>0</integer>"

        new_text = pattern.sub(_repl, text)
        if new_text != text:
            write_text(f, new_text)
            touched += 1
    if touched:
        log.info(f"fix_integers: {touched} archivos actualizados")


# --------------------------------------------------------------------------- #
# 5. Hebrew
# --------------------------------------------------------------------------- #

def fix_hebrew(res_root: Path) -> None:
    """
    Normaliza el locale hebreo: Android moderno usa values-iw; algunos APKs
    vienen con values-he. Se renombra values-he → values-iw.
    """
    if not res_root.is_dir():
        return
    he = res_root / "values-he"
    iw = res_root / "values-iw"
    if he.is_dir() and not iw.exists():
        try:
            he.rename(iw)
            log.info("fix_hebrew: values-he → values-iw")
        except Exception as e:
            log.warning(f"fix_hebrew: no se pudo renombrar: {e}")


# --------------------------------------------------------------------------- #
# 6. Manifest
# --------------------------------------------------------------------------- #

def fix_manifest(project_root: Path) -> None:
    """Asegura atributos mínimos y elimina los no soportados en AGP moderno."""
    mf = project_root / MANIFEST_REL
    if not mf.is_file():
        log.warning(f"fix_manifest: {mf} no existe")
        return
    try:
        text = read_text(mf)
    except Exception as e:
        log.warning(f"fix_manifest: no se pudo leer {mf}: {e}")
        return

    original = text
    # Elimina 'package=' del manifest (AGP lo gestiona vía namespace)
    text = re.sub(r'\s+package\s*=\s*"[^"]*"', "", text, count=1)

    if text != original:
        write_text(mf, text)
        log.info("fix_manifest: AndroidManifest.xml normalizado")


# --------------------------------------------------------------------------- #
# 7. Gradle
# --------------------------------------------------------------------------- #

APP_BUILD_GRADLE_TEMPLATE = """\
plugins {
    id 'com.android.application'
}

android {
    namespace 'com.example.rebuilt'
    compileSdk 34

    defaultConfig {
        applicationId "com.example.rebuilt"
        minSdk 21
        targetSdk 34
        versionCode 1
        versionName "1.0"
        multiDexEnabled true
    }

    compileOptions {
        sourceCompatibility JavaVersion.VERSION_17
        targetCompatibility JavaVersion.VERSION_17
    }

    buildTypes {
        release { minifyEnabled false }
        debug   { minifyEnabled false }
    }
}

dependencies {
    implementation 'androidx.appcompat:appcompat:1.7.0'
    implementation 'androidx.core:core-ktx:1.13.1'
    implementation 'androidx.constraintlayout:constraintlayout:2.1.4'
    implementation 'com.google.android.material:material:1.12.0'
    implementation 'androidx.multidex:multidex:2.0.1'
}
"""

ROOT_BUILD_GRADLE_TEMPLATE = """\
// Repositorio raíz — solo plugin management
"""

SETTINGS_TEMPLATE = """\
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.PREFER_SETTINGS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "NexusAPK"
include ':app'
"""

WRAPPER_PROPS_TEMPLATE = """\
distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\\://services.gradle.org/distributions/gradle-8.13-bin.zip
networkTimeout=10000
validateDistributionUrl=true
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
"""


def replace_gradle_files(project_root: Path) -> None:
    """Sustituye build.gradle, settings.gradle y gradle.properties por plantillas."""
    for rel, tpl in [
        (APP_GRADLE,  APP_BUILD_GRADLE_TEMPLATE),
        (ROOT_GRADLE, ROOT_BUILD_GRADLE_TEMPLATE),
        (SETTINGS,    SETTINGS_TEMPLATE),
    ]:
        p = project_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        write_text(p, tpl)
        log.info(f"replace_gradle_files: {rel} escrito")

    gp = project_root / GRADLE_PROPS
    if not gp.exists():
        write_text(gp, "android.useAndroidX=true\nandroid.enableJetifier=true\n")
        log.info("replace_gradle_files: gradle.properties creado")


def replace_wrapper(project_root: Path) -> None:
    """Reescribe gradle-wrapper.properties con Gradle 8.13."""
    wd = project_root / WRAPPER_DIR
    wd.mkdir(parents=True, exist_ok=True)
    write_text(project_root / WRAPPER_PROPS, WRAPPER_PROPS_TEMPLATE)
    log.info("replace_wrapper: gradle-wrapper.properties actualizado a 8.13")


# --------------------------------------------------------------------------- #
# Orquestador
# --------------------------------------------------------------------------- #

def apply_fixes(project_root: Path) -> None:
    res_root = project_root / RES_REL

    # 1. Recursos binarios
    remove_9patch(res_root)

    # 2. XML de values
    fix_plurals(res_root)         # ← PRIMERO (maneja CDATA interna de plurals)
    fix_cdata(res_root)
    fix_integers(res_root)
    fix_hebrew(res_root)

    # 3. Manifest
    fix_manifest(project_root)

    # 4. Gradle
    replace_gradle_files(project_root)
    replace_wrapper(project_root)

    # 5. Auditoría final — no modifica nada, solo reporta
    audit_plurals(res_root)


def main(argv):
    if len(argv) != 2:
        print(f"Uso: {argv[0]} <project_dir>", file=sys.stderr)
        return 2
    project_root = Path(argv[1]).resolve()
    if not project_root.is_dir():
        print(f"No es un directorio: {project_root}", file=sys.stderr)
        return 2
    apply_fixes(project_root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
