#!/usr/bin/env python3
"""
Minecraft Source Extractor
==========================
Downloads Minecraft JARs from Mojang, de-obfuscates (for 1.x) or decompiles
(for 26.x) using Fabric Loom's genSources task (Vineflower decompiler), and
extracts the full Java source tree to ~/minecraft_source/{version}.

Supports Minecraft full releases from 1.20.0 through 26.2.

Workflow (all versions)
-----------------------
1. A temporary Fabric Loom Gradle project is generated.
2. ./gradlew genSources:
   - Downloads the client + server JARs from Mojang.
   - For 1.x: merges JARs, remaps bytecode with official Mojang mappings,
     and decompiles with Vineflower.
   - For 26.x: Minecraft is not obfuscated. Loom merges JARs and decompiles
     directly (no mapping step needed).
3. The decompiled .java files are extracted from the Loom cache to the
   target directory.
4. The temporary project is removed (kept on failure for debugging).

Why Fabric Loom?
----------------
Fabric Loom is the standard build tool for Fabric mods. Its genSources task
has been battle-tested across all modern Minecraft versions. It handles:
  - Mojang authentication-free JAR downloads
  - Bytecode merging (client + server)
  - Official Mojang mapping application (for 1.x)
  - Vineflower decompilation
  - Caching (~/.gradle)

Requirements
------------
  - Python 3.9+
  - Java JDK (version depends on MC version: 17 for early 1.20, 21 for
    1.20.5+, 25 for 26.x)
  - Internet access (~200-400 MB download per version; subsequent runs are
    fast due to Gradle/Loom caching)

Usage
-----
  python minecraft_source_extractor.py --version 1.21.4
  python minecraft_source_extractor.py --version 26.2
  python minecraft_source_extractor.py --version 1.20.1 --output-dir /custom/path
  python minecraft_source_extractor.py --list-versions
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

LOADER = "0.19.3"          # Fabric Loader
LOOM = "1.17-SNAPSHOT"     # Fabric Loom Gradle plugin
GRADLE = "9.5.1"           # Gradle wrapper

# Minecraft version \E2\86\92 { java, fab }
VERSIONS: Dict[str, Dict[str, object]] = {
    "1.20":     {"java": 17, "fab": "0.83.0+1.20"},
    "1.20.1":   {"java": 17, "fab": "0.92.11+1.20.1"},
    "1.20.2":   {"java": 17, "fab": "0.91.6+1.20.2"},
    "1.20.3":   {"java": 17, "fab": "0.91.1+1.20.3"},
    "1.20.4":   {"java": 17, "fab": "0.97.3+1.20.4"},
    "1.20.5":   {"java": 21, "fab": "0.97.8+1.20.5"},
    "1.20.6":   {"java": 21, "fab": "0.100.8+1.20.6"},
    "1.21":     {"java": 21, "fab": "0.102.0+1.21"},
    "1.21.1":   {"java": 21, "fab": "0.116.15+1.21.1"},
    "1.21.2":   {"java": 21, "fab": "0.106.1+1.21.2"},
    "1.21.3":   {"java": 21, "fab": "0.114.1+1.21.3"},
    "1.21.4":   {"java": 21, "fab": "0.119.4+1.21.4"},
    "1.21.5":   {"java": 21, "fab": "0.128.2+1.21.5"},
    "1.21.6":   {"java": 21, "fab": "0.128.2+1.21.6"},
    "1.21.7":   {"java": 21, "fab": "0.129.0+1.21.7"},
    "1.21.8":   {"java": 21, "fab": "0.136.1+1.21.8"},
    "1.21.9":   {"java": 21, "fab": "0.134.1+1.21.9"},
    "1.21.10":  {"java": 21, "fab": "0.138.4+1.21.10"},
    "1.21.11":  {"java": 21, "fab": "0.141.6+1.21.11"},
    "26.1":     {"java": 25, "fab": "0.145.1+26.1"},
    "26.1.1":   {"java": 25, "fab": "0.145.4+26.1.1"},
    "26.1.2":   {"java": 25, "fab": "0.155.2+26.1.2"},
    "26.2":     {"java": 25, "fab": "0.156.0+26.2"},
}

DEFAULT_OUTPUT_ROOT = Path.home() / "minecraft_source"

# -----------------------------------------------------------------------------
# Small utilities
# -----------------------------------------------------------------------------

def is_26(mc: str) -> bool:
    return mc.startswith("26")


def check_java(required: int) -> Tuple[bool, str]:
    """Verify a suitable JDK is on PATH."""
    for cmd in ("javac", "java"):
        exe = shutil.which(cmd)
        if not exe:
            continue
        try:
            out = subprocess.check_output(
                [exe, "-version"], stderr=subprocess.STDOUT, text=True, timeout=10
            )
        except Exception:
            continue
        m = re.search(r"(\d+)", out.splitlines()[0])
        if m:
            found = int(m.group(1))
            if found >= required:
                return True, f"OK  ({cmd} version {found}, need >= {required})"
            return False, f"WARNING: {cmd} version {found}; need >= {required}"
        break
    return False, f"WARNING: No Java JDK on PATH (need >= {required})."


def make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | 0o755)


# -----------------------------------------------------------------------------
# Gradle project generation
# -----------------------------------------------------------------------------

def _build_gradle(mc: str, java: int, is26: bool) -> str:
    """
    Produce a minimal build.gradle that is accepted by Fabric Loom.
    1.x: fabric-loom-remap + official Mojang mappings
    26.x: fabric-loom (no remap, no mappings)
    """
    plugin = "net.fabricmc.fabric-loom" if is26 else "net.fabricmc.fabric-loom-remap"

    if is26:
        deps_block = """\
    minecraft "com.mojang:minecraft:${project.minecraft_version}"
    implementation "net.fabricmc:fabric-loader:${project.loader_version}"
    implementation "net.fabricmc.fabric-api:fabric-api:${project.fabric_api_version}"
"""
    else:
        deps_block = """\
    minecraft "com.mojang:minecraft:${project.minecraft_version}"
    mappings loom.officialMojangMappings()
    modImplementation "net.fabricmc:fabric-loader:${project.loader_version}"
    modImplementation "net.fabricmc.fabric-api:fabric-api:${project.fabric_api_version}"
"""

    return f"""plugins {{
    id '{plugin}' version "${{loom_version}}"
    id 'maven-publish'
}}

version = "1.0.0"
group = "com.example"

repositories {{
}}

loom {{
    splitEnvironmentSourceSets()
    mods {{
        "modid" {{
            sourceSet sourceSets.main
            sourceSet sourceSets.client
        }}
    }}
}}

dependencies {{
{deps_block}}}

tasks.withType(JavaCompile).configureEach {{
    it.options.release = {java}
}}

java {{
    withSourcesJar()
    sourceCompatibility = JavaVersion.VERSION_{java}
    targetCompatibility = JavaVersion.VERSION_{java}
}}
"""


def _settings_gradle() -> str:
    return """pluginManagement {
    repositories {
        maven { name = 'Fabric'; url = 'https://maven.fabricmc.net/' }
        mavenCentral()
        gradlePluginPortal()
    }
}
rootProject.name = 'mc-source-extractor'
"""


def _gradle_properties(mc: str, fab: str) -> str:
    return f"""org.gradle.jvmargs=-Xmx4G
org.gradle.parallel=true
org.gradle.configuration-cache=false

minecraft_version={mc}
loader_version={LOADER}
loom_version={LOOM}
mod_id=modid
mod_name=mc-source-extractor
fabric_api_version={fab}
"""


def _gradle_wrapper_props() -> str:
    return (
        "distributionBase=GRADLE_USER_HOME\n"
        "distributionPath=wrapper/dists\n"
        f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{GRADLE}-bin.zip\n"
        "zipStoreBase=GRADLE_USER_HOME\n"
        "zipStorePath=wrapper/dists\n"
    )


def _fetch_url(url: str, dest: Path, *, executable: bool = False) -> None:
    """Download *url* to *dest*.  Raise on HTTP / IO error."""
    req = urllib.request.Request(url, headers={"User-Agent": "MinecraftSourceExtractor/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    if executable:
        make_executable(dest)


def _generate_gradle_project(project_dir: Path, mc: str, java: int, fab: str) -> None:
    """Write a minimal Fabric Loom project to *project_dir*."""
    project_dir.mkdir(parents=True, exist_ok=True)

    is26 = is_26(mc)

    (project_dir / "build.gradle").write_text(
        _build_gradle(mc, java, is26), encoding="utf-8"
    )
    (project_dir / "settings.gradle").write_text(_settings_gradle(), encoding="utf-8")
    (project_dir / "gradle.properties").write_text(
        _gradle_properties(mc, fab), encoding="utf-8"
    )

    wrapper_dir = project_dir / "gradle" / "wrapper"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    (wrapper_dir / "gradle-wrapper.properties").write_text(
        _gradle_wrapper_props(), encoding="utf-8"
    )

    # Download wrapper assets from the reference Fabric example-mod repo
    base = "https://raw.githubusercontent.com/FabricMC/fabric-example-mod/26.2"
    _fetch_url(f"{base}/gradle/wrapper/gradle-wrapper.jar", wrapper_dir / "gradle-wrapper.jar")
    _fetch_url(f"{base}/gradlew", project_dir / "gradlew", executable=True)

    # Minimal sources so Loom doesn't complain
    src_main = project_dir / "src" / "main" / "java" / "com" / "example"
    src_main.mkdir(parents=True, exist_ok=True)
    (src_main / "DummyMod.java").write_text(
        "package com.example;\npublic class DummyMod {}\n", encoding="utf-8"
    )

    src_client = project_dir / "src" / "client" / "java" / "com" / "example" / "client"
    src_client.mkdir(parents=True, exist_ok=True)
    (src_client / "DummyClient.java").write_text(
        "package com.example.client;\npublic class DummyClient {}\n", encoding="utf-8"
    )

    res_dir = project_dir / "src" / "main" / "resources"
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / "fabric.mod.json").write_text(json.dumps({
        "schemaVersion": 1,
        "id": "modid",
        "version": "1.0.0",
        "name": "mc-source-extractor",
        "entrypoints": {
            "main": ["com.example.DummyMod"],
            "client": ["com.example.client.DummyClient"],
        },
    }, indent=2), encoding="utf-8")


# -----------------------------------------------------------------------------
# Run genSources
# -----------------------------------------------------------------------------

def _run_gen_sources(project_dir: Path) -> int:
    """Run ./gradlew genSources.  Return exit code."""
    gradlew = project_dir / "gradlew"
    print("[*] Running ./gradlew genSources \E2\80\A6")
    print(f"    This will download Minecraft JARs and decompile them.")
    print(f"    First run may take 5-15 minutes. Subsequent runs are faster.\n")

    env = os.environ.copy()
    result = subprocess.run(
        [str(gradlew), "genSources", "--no-daemon", "--stacktrace"],
        cwd=project_dir,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=False,
    )
    return result.returncode


# -----------------------------------------------------------------------------
# Find and extract decompiled sources from the Loom cache
# -----------------------------------------------------------------------------

def _find_source_jars(project_dir: Path, mc: str) -> List[Path]:
    """
    Locate all -sources.jar files produced by Loom's genSources.

    Loom 1.17+ produces two source JARs:
      - minecraft-common-<hash>-<ver>-sources.jar   (shared game code)
      - minecraft-clientOnly-<hash>-<ver>-sources.jar (client-only code)

    Older Loom versions may produce a single:
      - merged-<ver>-sources.jar

    We also accept any *-sources.jar >= 500 KB that lives under the
    project's Loom cache.
    """
    cache_root = project_dir / ".gradle" / "loom-cache"
    if not cache_root.exists():
        return []

    jars: List[Path] = []
    seen = set()

    for jar in cache_root.rglob("*-sources.jar"):
        if jar.stat().st_size < 500_000:
            continue
        name = jar.name
        # Deduplicate by name so we don't pick up the same JAR twice
        if name in seen:
            continue
        seen.add(name)

        # Prefer naming patterns we know about
        if (f"-common-" in name or f"-clientOnly-" in name) and mc in name:
            jars.append(jar)
        elif name.startswith("merged-") and mc in name:
            jars.append(jar)

    # Fallback: if we didn't match anything by name, grab any large source jar
    if not jars:
        for jar in cache_root.rglob("*-sources.jar"):
            if jar.stat().st_size >= 500_000 and jar.name not in seen:
                seen.add(jar.name)
                jars.append(jar)

    return jars


def _extract_java_files(source_jars: List[Path], output_dir: Path) -> int:
    """Extract all .java files from *source_jars* into *output_dir*.
    Later JARs in the list may overwrite earlier ones (clientOnly over common)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for source_jar in source_jars:
        count = 0
        with zipfile.ZipFile(source_jar, "r") as zf:
            for member in zf.namelist():
                if member.endswith(".java") and not member.startswith("META-INF"):
                    dest_path = output_dir / member
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(dest_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    count += 1
        print(f"    {source_jar.name}: {count} .java files")
        total += count
    return total


# -----------------------------------------------------------------------------
# Main extraction routine
# -----------------------------------------------------------------------------

def extract_sources(mc_version: str, output_root: Optional[Path] = None) -> int:
    """
    Download and de-obfuscate/decompile Minecraft source for *mc_version*.

    Parameters
    ----------
    mc_version : str
        Full release version (e.g. "1.21.4", "26.2").
    output_root : Path, optional
        Root directory.  Sources land in ``<output_root>/<mc_version>/``.
        Default: ``~/minecraft_source/``.

    Returns
    -------
    int
        0 on success, non-zero on error.
    """
    if mc_version not in VERSIONS:
        print(f"ERROR: Unsupported version '{mc_version}'.", file=sys.stderr)
        print("Use --list-versions to see supported versions.", file=sys.stderr)
        return 2

    info = VERSIONS[mc_version]
    java_ver: int = int(info["java"])     # type: ignore[arg-type]
    fab_ver: str = str(info["fab"])       # type: ignore[arg-type]

    output_root = output_root or DEFAULT_OUTPUT_ROOT
    output_dir = output_root / mc_version

    # Java check
    ok, msg = check_java(java_ver)
    print(f"Java check for {mc_version}: {msg}")
    if not ok:
        print("A suitable JDK is required to continue.", file=sys.stderr)
        return 1

    if output_dir.exists() and any(output_dir.iterdir()):
        print(f"[!] Output directory {output_dir} already exists and is non-empty.")
        print(f"    Existing .java files will be overwritten.\n")

    work_dir = Path(tempfile.mkdtemp(prefix=f"mc_src_{mc_version.replace('.', '_')}_"))
    print(f"[*] Temp working directory: {work_dir}")

    try:
        # 1 \E2\80\94 Generate Gradle project
        print("[*] Generating Gradle project \E2\80\A6")
        _generate_gradle_project(work_dir, mc_version, java_ver, fab_ver)

        # 2 \E2\80\94 Run genSources
        rc = _run_gen_sources(work_dir)
        if rc != 0:
            print(f"\nERROR: genSources failed (exit code {rc}).", file=sys.stderr)
            print(f"The Gradle project was kept at {work_dir} for debugging.", file=sys.stderr)
            return rc

        # 3 \E2\80\94 Locate the source JAR(s)
        print("\n[*] Locating decompiled source JARs in Loom cache \E2\80\A6")
        source_jars = _find_source_jars(work_dir, mc_version)
        if not source_jars:
            print("ERROR: Could not find any -sources.jar in Loom cache.", file=sys.stderr)
            print(f"Gradle project kept at {work_dir} for debugging.", file=sys.stderr)
            return 1

        for jar in source_jars:
            print(f"[*] Found: {jar}  ({jar.stat().st_size / 1024 / 1024:.1f} MB)")

        # 4 \E2\80\94 Extract
        print(f"\n[*] Extracting .java files to {output_dir} \E2\80\A6")
        count = _extract_java_files(source_jars, output_dir)
        print(f"[\E2\9C\93] Extracted {count} .java files to {output_dir}")

        # 5 \E2\80\94 Cleanup
        print(f"[*] Cleaning up temporary project \E2\80\A6")
        shutil.rmtree(work_dir, ignore_errors=True)
        return 0

    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        print(f"Temporary project kept at {work_dir} for debugging.", file=sys.stderr)
        return 1


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def list_versions() -> None:
    print("Supported Minecraft full releases (1.20.0 \E2\86\92 26.2):")
    for v in VERSIONS:
        tag = "  (26.x: no obfuscation)" if is_26(v) else ""
        print(f"  {v}{tag}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download and de-obfuscate Minecraft Java source code."
    )
    p.add_argument("--version", help="Minecraft full release (e.g. 1.21.4, 26.2)")
    p.add_argument(
        "--output-dir",
        default=None,
        help="Parent directory for extracted sources. Default: ~/minecraft_source",
    )
    p.add_argument(
        "--list-versions", action="store_true",
        help="List all supported versions and exit.",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.list_versions:
        list_versions()
        return 0
    if not args.version:
        print("ERROR: --version is required.  Use --help for usage.", file=sys.stderr)
        return 2
    output_root = Path(args.output_dir) if args.output_dir else None
    return extract_sources(args.version, output_root)


if __name__ == "__main__":
    sys.exit(main())
