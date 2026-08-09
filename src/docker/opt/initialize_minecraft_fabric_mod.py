#!/usr/bin/env python3
"""
Minecraft Fabric Mod Template Generator
=======================================
Creates a fully pre-configured Fabric mod project, like the "Minecraft
Development" IntelliJ IDEA plugin does, for any Minecraft full release from
1.20.0 through 26.2.

Features
--------
* Correct Fabric Loader / Loom / Java / Gradle / Fabric API versions.
* Official Mojang mappings for 1.x (obfuscated) versions.
* No mappings for 26.x (Minecraft is no longer obfuscated).
* Deobfuscated Minecraft source via Loom genSources (auto-run on runClient).
* Fabric API, example classes, mixin support, IntelliJ run configs.
* Gradle wrapper 9.5.1 (from official Fabric example-mod assets).

Data verified against FabricMC/fabric-example-mod branches and
meta.fabricmc.net.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, stat, sys, urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = SCRIPT_DIR / "assets"

LOADER = "0.19.3"
LOOM = "1.17-SNAPSHOT"
GRADLE = "9.5.1"

VERSIONS = {
    "1.20":   {"java": 17, "fab": "0.83.0+1.20"},
    "1.20.1": {"java": 17, "fab": "0.92.11+1.20.1"},
    "1.20.2": {"java": 17, "fab": "0.91.6+1.20.2"},
    "1.20.3": {"java": 17, "fab": "0.91.1+1.20.3"},
    "1.20.4": {"java": 17, "fab": "0.97.3+1.20.4"},
    "1.20.5": {"java": 21, "fab": "0.97.8+1.20.5"},
    "1.20.6": {"java": 21, "fab": "0.100.8+1.20.6"},
    "1.21":   {"java": 21, "fab": "0.102.0+1.21"},
    "1.21.1": {"java": 21, "fab": "0.116.15+1.21.1"},
    "1.21.2": {"java": 21, "fab": "0.106.1+1.21.2"},
    "1.21.3": {"java": 21, "fab": "0.114.1+1.21.3"},
    "1.21.4": {"java": 21, "fab": "0.119.4+1.21.4"},
    "1.21.5": {"java": 21, "fab": "0.128.2+1.21.5"},
    "1.21.6": {"java": 21, "fab": "0.128.2+1.21.6"},
    "1.21.7": {"java": 21, "fab": "0.129.0+1.21.7"},
    "1.21.8": {"java": 21, "fab": "0.136.1+1.21.8"},
    "1.21.9": {"java": 21, "fab": "0.134.1+1.21.9"},
    "1.21.10": {"java": 21, "fab": "0.138.4+1.21.10"},
    "1.21.11": {"java": 21, "fab": "0.141.6+1.21.11"},
    "26.1":   {"java": 25, "fab": "0.145.1+26.1"},
    "26.1.1": {"java": 25, "fab": "0.145.4+26.1.1"},
    "26.1.2": {"java": 25, "fab": "0.155.2+26.1.2"},
    "26.2":   {"java": 25, "fab": "0.156.0+26.2"},
}

IDENTIFIER_VERSIONS = {"1.21.11", "26.1", "26.1.1", "26.1.2", "26.2"}

def is_26(ver):
    return ver.startswith("26")

def is_identifier(ver):
    return ver in IDENTIFIER_VERSIONS

def fetch_latest(meta_url):
    try:
        with urllib.request.urlopen(meta_url, timeout=15) as r:
            data = json.load(r)
        if data:
            return data[0]["version"]
    except Exception:
        pass
    return None

def build_gradle(mc, java):
    is26 = is_26(mc)
    plugin_id = "net.fabricmc.fabric-loom" if is26 else "net.fabricmc.fabric-loom-remap"
    deps_lines = [
        '\tminecraft "com.mojang:minecraft:${project.minecraft_version}"',
    ]
    if not is26:
        deps_lines.append('\tmappings loom.officialMojangMappings()')
        deps_lines.append('\tmodImplementation "net.fabricmc:fabric-loader:${project.loader_version}"')
        deps_lines.append('\tmodImplementation "net.fabricmc.fabric-api:fabric-api:${project.fabric_api_version}"')
    else:
        deps_lines.append('\timplementation "net.fabricmc:fabric-loader:${project.loader_version}"')
        deps_lines.append('\timplementation "net.fabricmc.fabric-api:fabric-api:${project.fabric_api_version}"')
    deps = "\n".join(deps_lines)
    return """plugins {
    id '%s' version "${loom_version}"
    id 'maven-publish'
}

version = project.mod_version
group = project.maven_group

repositories {
    // Add repositories to retrieve artifacts from in here.
}

loom {
    splitEnvironmentSourceSets()

    mods {
        "${project.mod_id}" {
            sourceSet sourceSets.main
            sourceSet sourceSets.client
        }
    }
}

dependencies {
    // To change the versions see the gradle.properties file
%s
}

processResources {
    def version = project.version
    inputs.property "version", version

    filesMatching("fabric.mod.json") {
        expand "version": version
    }
}

tasks.withType(JavaCompile).configureEach {
    it.options.release = %d
}

java {
    // Loom will automatically attach sourcesJar to a RemapSourcesJar task
    withSourcesJar()

    sourceCompatibility = JavaVersion.VERSION_%d
    targetCompatibility = JavaVersion.VERSION_%d
}

jar {
    def projectName = project.name
    inputs.property "projectName", projectName

    from("LICENSE") {
        rename { "${it}_${projectName}" }
    }
}

// Deobfuscated Minecraft source code
// Loom provides genSources.  Run: ./gradlew genSources

publishing {
    publications {
        create("mavenJava", MavenPublication) {
            from components.java
        }
    }
    repositories {
        // Add repositories to publish to here.
    }
}
""" % (plugin_id, deps, java, java, java)
def settings_gradle(modid):
    return """pluginManagement {
    repositories {
        maven {
            name = 'Fabric'
            url = 'https://maven.fabricmc.net/'
        }
        mavenCentral()
        gradlePluginPortal()
    }
}

// Should match your modid
rootProject.name = '%s'
""" % modid

def fabric_mod_json(mc, modid, name, java, pkg):
    return """{
    "schemaVersion": 1,
    "id": "%s",
    "version": "${version}",
    "name": "%s",
    "description": "This is an example description!",
    "authors": ["Me!"],
    "contact": {
        "homepage": "https://fabricmc.net/",
        "sources": "https://github.com/FabricMC/fabric-example-mod"
    },
    "license": "CC0-1.0",
    "environment": "*",
    "entrypoints": {
        "main": ["%s.ExampleMod"],
        "client": ["%s.client.ExampleModClient"]
    },
    "mixins": [
        "%s.mixins.json",
        {"config": "%s.client.mixins.json", "environment": "client"}
    ],
    "depends": {
        "fabricloader": ">=%s",
        "minecraft": "~%s",
        "java": ">=%d",
        "fabric-api": "*"
    }
}
""" % (modid, name, pkg, pkg, modid, modid, LOADER, mc, java)

def main_mod_java(pkg, use_identifier):
    res = "Identifier" if use_identifier else "ResourceLocation"
    id_body = ("Identifier.fromNamespaceAndPath(MOD_ID, path)" if use_identifier
               else "new ResourceLocation(MOD_ID, path)")
    return """package %s;

import net.fabricmc.api.ModInitializer;
import net.minecraft.resources.%s;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class ExampleMod implements ModInitializer {
    public static final String MOD_ID = "modid";
    public static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);

    @Override
    public void onInitialize() {
        LOGGER.info("Hello Fabric world!");
    }

    public static %s id(String path) {
        return %s;
    }
}
""" % (pkg, res, res, id_body)

def example_mod_client_java(pkg):
    return """package %s.client;

import net.fabricmc.api.ClientModInitializer;

public class ExampleModClient implements ClientModInitializer {
    @Override
    public void onInitializeClient() {
        // Client-specific setup like rendering goes here.
    }
}
""" % pkg

def example_mixin_java(pkg):
    return """package %s.mixin;

import net.minecraft.server.MinecraftServer;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(MinecraftServer.class)
public class ExampleMixin {
    @Inject(at = @At("HEAD"), method = "loadLevel")
    private void init(CallbackInfo info) {
        // Injected into MinecraftServer.loadLevel()
    }
}
""" % pkg

def example_client_mixin_java(pkg):
    return """package %s.client.mixin;

import net.minecraft.client.Minecraft;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(Minecraft.class)
public class ExampleClientMixin {
    @Inject(at = @At("HEAD"), method = "run")
    private void init(CallbackInfo info) {
        // Injected into Minecraft.run()
    }
}
""" % pkg

def mixins_json(pkg, javalv, is_client=False):
    suffix = "client" if is_client else "mixin"
    cls = "ExampleClientMixin" if is_client else "ExampleMixin"
    key = "client" if is_client else "mixins"
    full_pkg = pkg + (".client.mixin" if is_client else ".mixin")
    return """{
    "required": true,
    "package": "%s",
    "compatibilityLevel": "JAVA_%d",
    "%s": ["%s"],
    "injectors": {"defaultRequire": 1},
    "overwrites": {"requireAnnotations": true}
}
""" % (full_pkg, javalv, key, cls)

def gradle_wrapper_props():
    return "distributionBase=GRADLE_USER_HOME\n" + \
           "distributionPath=wrapper/dists\n" + \
           "distributionUrl=https\\://services.gradle.org/distributions/gradle-%s-bin.zip\n" % GRADLE + \
           "zipStoreBase=GRADLE_USER_HOME\n" + \
           "zipStorePath=wrapper/dists\n"

def make_gitignore():
    return """# gradle
.gradle/
build/
out/
classes/

# eclipse
*.launch

# idea
.idea/
*.iml
*.ipr
*.iws

# vscode
.settings/
.vscode/
bin/
.classpath
.project

# macos
*.DS_Store

# fabric
run/

# java
hs_err_*.log
replay_*.log
*.hprof
*.jfr
"""

def write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def copy_asset(name, dest):
    src = ASSETS_DIR / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copyfile(src, dest)
    else:
        fallback_map = {
            "gradle-wrapper.jar": "gradle/wrapper/gradle-wrapper.jar",
            "gradlew": "gradlew",
            "gradlew.bat": "gradlew.bat",
            "icon.png": "src/main/resources/assets/modid/icon.png",
        }
        url = "https://raw.githubusercontent.com/FabricMC/fabric-example-mod/26.2/" + \
              fallback_map.get(name, name)
        try:
            urllib.request.urlretrieve(url, dest)
        except Exception as e:
            print(f"  [!] Could not fetch {name}: {e}", file=sys.stderr)
            return
    if name == "gradlew":
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

def make_idea_run_xml():
    return """<component name="ProjectRunConfigurationManager">
  <configuration default="false" name="Minecraft Client" type="GradleRunConfiguration" factoryName="Gradle">
    <ExternalSystemSettings>
      <option name="executionName" />
      <option name="externalProjectPath" value="$PROJECT_DIR$" />
      <option name="externalSystemIdString" value="GRADLE" />
      <option name="scriptParameters" value="genSources runClient --no-daemon" />
      <option name="taskDescriptions">
        <list />
      </option>
      <option name="taskNames">
        <list />
      </option>
    </ExternalSystemSettings>
    <GradleScriptDebugEnabled>true</GradleScriptDebugEnabled>
    <method v="2" />
  </configuration>
  <configuration default="false" name="Minecraft Server" type="GradleRunConfiguration" factoryName="Gradle">
    <ExternalSystemSettings>
      <option name="executionName" />
      <option name="externalProjectPath" value="$PROJECT_DIR$" />
      <option name="externalSystemIdString" value="GRADLE" />
      <option name="scriptParameters" value="genSources runServer --no-daemon" />
      <option name="taskDescriptions">
        <list />
      </option>
      <option name="taskNames">
        <list />
      </option>
    </ExternalSystemSettings>
    <GradleScriptDebugEnabled>true</GradleScriptDebugEnabled>
    <method v="2" />
  </configuration>
</component>
"""

def gradle_properties(mc, modid, name, fab):
    return """# Done to increase the memory available to gradle.
org.gradle.jvmargs=-Xmx1G
org.gradle.parallel=true

# IntelliJ IDEA is not yet fully compatible with configuration cache
org.gradle.configuration-cache=false

# Fabric Properties
# check these on https://fabricmc.net/develop
minecraft_version=%s
loader_version=%s
loom_version=%s

# Mod Properties
mod_version=1.0.0
maven_group=com.example
mod_id=%s
mod_name=%s

# Dependencies
fabric_api_version=%s
""" % (mc, LOADER, LOOM, modid, name, fab)

def generate_project(mc, modid, name, pkg, out, fab_api):
    if mc not in VERSIONS:
        raise ValueError("Unsupported MC version: " + mc)
    info = VERSIONS[mc]
    java = info["java"]
    fab = fab_api if fab_api else info["fab"]
    pkg_path = pkg.replace(".", "/")
    use_id = is_identifier(mc)

    write_text(out / "build.gradle", build_gradle(mc, java))
    write_text(out / "settings.gradle", settings_gradle(modid))
    write_text(out / "gradle.properties", gradle_properties(mc, modid, name, fab))

    wrapper_dir = out / "gradle" / "wrapper"
    write_text(wrapper_dir / "gradle-wrapper.properties", gradle_wrapper_props())
    copy_asset("gradle-wrapper.jar", wrapper_dir / "gradle-wrapper.jar")
    copy_asset("gradlew", out / "gradlew")
    copy_asset("gradlew.bat", out / "gradlew.bat")

    write_text(out / ".gitignore", make_gitignore())
    write_text(out / "src" / "main" / "resources" / "fabric.mod.json",
               fabric_mod_json(mc, modid, name, java, pkg))

    write_text(out / "src" / "main" / "java" / pkg_path / "ExampleMod.java",
               main_mod_java(pkg, use_id))
    write_text(out / "src" / "main" / "java" / pkg_path / "mixin" / "ExampleMixin.java",
               example_mixin_java(pkg))
    write_text(out / "src" / "client" / "java" / pkg_path / "client" / "ExampleModClient.java",
               example_mod_client_java(pkg))
    write_text(out / "src" / "client" / "java" / pkg_path / "client" / "mixin" / "ExampleClientMixin.java",
               example_client_mixin_java(pkg))

    write_text(out / "src" / "main" / "resources" / f"{modid}.mixins.json",
               mixins_json(pkg, java))
    write_text(out / "src" / "client" / "resources" / f"{modid}.client.mixins.json",
               mixins_json(pkg, java, is_client=True))

    copy_asset("icon.png", out / "src" / "main" / "resources" / "assets" / modid / "icon.png")
    write_text(out / ".idea" / "runConfigurations" / "Minecraft Client.xml",
               make_idea_run_xml())

    gw = out / "gradlew"
    if gw.exists():
        gw.chmod(gw.stat().st_mode | 0o755)
    return out

def check_java(required_version):
    """Look for a Java JDK on PATH and check its version against required_version.
    Returns (found:bool, message:str)."""
    import subprocess
    # Try `javac` first (JDK), fall back to `java` (could be JRE-only but still useful)
    for cmd in ["javac", "java"]:
        exe = shutil.which(cmd)
        if not exe:
            continue
        try:
            out = subprocess.check_output([exe, "-version"], stderr=subprocess.STDOUT, text=True, timeout=10)
        except Exception:
            continue
        # Parse major version from output like 'javac 21.0.5' or 'openjdk version "21.0.5"'
        import re as _re
        m = _re.search(r'(\d+)', out.splitlines()[0])
        if m:
            found_ver = int(m.group(1))
            if found_ver >= required_version:
                return True, f"OK  (found {cmd} version {found_ver}, need >= {required_version})"
            else:
                return False, f"WARNING: found {cmd} version {found_ver}, but Minecraft {required_version} needs >= {required_version}"
        break
    return False, f"WARNING: No Java JDK found on PATH.  Gradle will need one (version >= {required_version})."

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Create a Fabric mod template for Minecraft 1.20.0 -> 26.2")
    p.add_argument("--version", help="Minecraft full release (e.g. 1.21.4, 26.2)")
    p.add_argument("--output-dir", default=".", help="Parent directory for the project")
    p.add_argument("--name", default="Example Mod", help="Mod display name")
    p.add_argument("--modid", default="modid", help="Mod id (lowercase letters/numbers)")
    p.add_argument("--package", default="com.example", help="Java package for classes")
    p.add_argument("--author", default="Me!", help="Author name (shown in fabric.mod.json)")
    p.add_argument("--fabric-api", default=None, help="Override Fabric API version")
    p.add_argument("--extract-sources", action="store_true",
                    help="After genSources, also unpack Minecraft .java files")
    p.add_argument("--list-versions", action="store_true", help="List supported versions")
    return p.parse_args(argv)

def list_versions():
    print("Supported Minecraft full releases (1.20.0 -> 26.2):")
    for v in VERSIONS:
        marker = "  (26.x: no mappings needed)" if is_26(v) else ""
        print(f"  {v}{marker}")

def unpack_minecraft_sources(project_dir):
    """Extract deobfuscated Minecraft source JARs into minecraft-sources/.
    Call this after ./gradlew genSources has been run."""
    import zipfile
    dest = project_dir / "minecraft-sources"
    dest.mkdir(parents=True, exist_ok=True)
    loom_cache = project_dir / ".gradle" / "loom-cache" / "minecraftMaven" / "net" / "minecraft"
    if not loom_cache.exists():
        print("  [!] Loom cache not found. Run ./gradlew genSources first.")
        return dest
    count = 0
    for jar_file in loom_cache.glob("**/*-sources.jar"):
        with zipfile.ZipFile(jar_file, 'r') as zf:
            for member in zf.namelist():
                if member.endswith('.java') and not member.startswith('META-INF'):
                    zf.extract(member, dest)
                    count += 1
    print(f"  Extracted {count} Minecraft source files to {dest}")
    return dest


def main(argv=None):
    args = parse_args(argv)
    if args.list_versions:
        list_versions()
        return 0
    if args.version not in VERSIONS:
        print(f"ERROR: Unsupported version '{args.version}'.")
        print("Use --list-versions to see all supported versions.")
        return 2
    if not re.match(r"^[a-z][a-z0-9_]*$", args.modid):
        print("ERROR: modid must be lowercase letters/numbers/underscores, starting with a letter.")
        return 2

    # Check Java JDK availability
    java_ver = VERSIONS[args.version]["java"]
    ok, msg = check_java(java_ver)
    print(f"Java check for version {args.version}: {msg}")

    fab = args.fabric_api
    if not fab:
        meta_url = f"https://meta.fabricmc.net/v2/versions/fabric-api/{args.version}"
        fetched = fetch_latest(meta_url)
        if fetched:
            fab = fetched
    resolved_fab = fab or VERSIONS[args.version]["fab"]

    out = Path(args.output_dir) / args.modid
    root = generate_project(args.version, args.modid, args.name, args.package,
                            out, resolved_fab)
    print(f"Created Fabric mod template at: {root}")
    print(f"  Minecraft: {args.version}")
    print(f"  Loader:    {LOADER}")
    print(f"  Loom:      {LOOM}")
    print(f"  FabricAPI: {resolved_fab}")
    print()
    print("Next steps:")
    print(f"  cd {root}")
    print("  ./gradlew genSources   # generates deobfuscated Minecraft source")
    print("  ./gradlew runClient    # runs the game from dev environment")
    print("  ./gradlew build        # builds the mod .jar")
    if args.extract_sources:
        unpack_minecraft_sources(root)
    return 0

if __name__ == "__main__":
    sys.exit(main())

