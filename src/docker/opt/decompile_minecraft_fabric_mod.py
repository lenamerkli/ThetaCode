#!/usr/bin/env python3
"""
Minecraft Fabric Mod Decompiler
Decompiles Fabric mod JARs for Minecraft 1.20.0 - 26.2.

For obfuscated 1.x:
  1. Downloads Mojang client_mappings (ProGuard: official->obf)
  2. Downloads Fabric Intermediary (Tiny v2: obf->intermediary)
  3. Chains into intermediary->official Tiny v2
  4. Downloads Minecraft client.jar
  5. Remaps mod JAR with tiny-remapper (client.jar on classpath so
     external Minecraft references ARE remapped)
  6. Decompiles with CFR

For unobfuscated 26.x: decompiles directly with CFR.

Usage:
  python3 fabric_mod_decompiler.py mod.jar [-v VERSION] [-o OUTDIR]
  python3 fabric_mod_decompiler.py --list
"""

import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, urllib.error, urllib.request, zipfile
from pathlib import Path

VM_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
CFR_URL = "https://github.com/leibnitz27/cfr/releases/download/0.152/cfr-0.152.jar"
IM_BASE = "https://maven.fabricmc.net/net/fabricmc/intermediary"
TR_BASE = "https://maven.fabricmc.net/net/fabricmc/tiny-remapper"
TR_VER = "0.14.0"
CACHE = Path.home() / ".cache" / "fabric_mod_decompiler"


# ---- helpers ----

def _cache(p=None):
    d = Path(p) if p else CACHE
    d.mkdir(parents=True, exist_ok=True)
    return d


def _http(url):
    req = urllib.request.Request(url, headers={"User-Agent": "FMD/1.0"})
    with urllib.request.urlopen(req) as r: return r.read()


def _json(url):
    raw = _http(url)
    try:
        return json.loads(raw.decode("utf-8"))
    except:
        return json.loads(raw.decode("utf-8-sig"))


def _dl(url, dest): urllib.request.urlretrieve(url, str(dest))


def sha1_ok(path, exp=None):
    if not exp: return True
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while c := f.read(8192): h.update(c)
    return h.hexdigest() == exp


def ver(v):
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", v)
    return (int(m[1]), int(m[2]), int(m[3] or 0)) if m else None


def is_obf(v): p = ver(v); return p and p[0] == 1


def is_sup(v): p = ver(v); return p and ((p[0] == 1 and p[1] >= 20) or p[0] >= 26)


def all_releases(): return [x["id"] for x in _json(VM_URL)["versions"] if x["type"] == "release"]


def mod_mc(jar):
    try:
        with zipfile.ZipFile(jar, "r") as z:
            if "fabric.mod.json" in z.namelist():
                d = json.loads(z.read("fabric.mod.json"))
                mc = str(d.get("depends", {}).get("minecraft", ""))
                m = re.match(r"[>=^~]*\s*(\d+\.\d+(?:\.\d+)?)", mc)
                return m.group(1) if m else mc.strip()
    except:
        pass
    return None


def get_cfr(cache):
    j = cache / "cfr.jar"
    if not j.exists(): print("[*] Downloading CFR..."); _dl(CFR_URL, j)
    return j


def get_tr(cache):
    j = cache / f"tiny-remapper-{TR_VER}-fat.jar"
    if not j.exists(): print(f"[*] Downloading tiny-remapper..."); _dl(
        f"{TR_BASE}/{TR_VER}/tiny-remapper-{TR_VER}-fat.jar", j)
    return j


def get_ver_info(vid):
    for v in _json(VM_URL)["versions"]:
        if v["id"] == vid: return _json(v["url"])
    raise ValueError(f"Version {vid} not found")


def dl_mojang(vid, info, cache):
    cm = info.get("downloads", {}).get("client_mappings")
    if not cm: return None
    dest = cache / f"mojang_{vid}.txt"
    if dest.exists() and sha1_ok(dest, cm.get("sha1")):
        print(f"[+] Using cached Mojang mappings")
        return dest
    print(f"[*] Downloading Mojang mappings for {vid}...")
    _dl(cm["url"], dest)
    return dest


def dl_inter(vid, cache):
    tiny = cache / f"inter_{vid}.tiny"
    if tiny.exists():
        print(f"[+] Using cached intermediary")
        return tiny
    jar_url = f"{IM_BASE}/{vid}/intermediary-{vid}-v2.jar"
    jar_tmp = cache / f"inter_{vid}.jar"
    print(f"[*] Downloading intermediary for {vid}...")
    try:
        _dl(jar_url, jar_tmp)
    except urllib.error.HTTPError as e:
        print(f"[!] Intermediary not available: {e}")
        return None
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(jar_tmp, "r") as zf: zf.extract("mappings/mappings.tiny", td)
        shutil.move(os.path.join(td, "mappings", "mappings.tiny"), str(tiny))
    jar_tmp.unlink()
    return tiny


def dl_client(vid, info, cache):
    """Download Minecraft client JAR (needed as classpath for remapping)."""
    client = cache / f"client_{vid}.jar"
    cinfo = info.get("downloads", {}).get("client")
    if not cinfo: return None
    if client.exists() and sha1_ok(client, cinfo.get("sha1")):
        print(f"[+] Using cached client.jar")
        return client
    print(f"[*] Downloading Minecraft client.jar {vid}...")
    _dl(cinfo["url"], client)
    return client


# ---- Mapping chain ----

def parse_pg_inverted(path):
    """Parse Mojang ProGuard file. Format: official.Class -> obf:
       Returns {obfuscated_name: official_name}."""
    result = {}
    cur_off = ""
    cur_obf = ""
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"): continue
            has_indent = raw.startswith("    ")
            if " -> " in stripped:
                left, _, right = stripped.partition(" -> ")
                if not has_indent:
                    cur_off = left.strip()
                    cur_obf = right.strip().rstrip(":")
                    result[cur_obf] = cur_off
                else:
                    obf_mem = right.strip()
                    toks = left.split()
                    off_mem = ""
                    if toks and ":" in toks[0]:
                        if len(toks) >= 3:
                            off_mem = toks[2].split("(")[0]
                        elif toks:
                            off_mem = toks[-1].split("(")[0]
                    elif toks:
                        off_mem = toks[-1]
                    if off_mem: result[f"{cur_obf}.{obf_mem}"] = f"{cur_off}.{off_mem}"
    return result


def parse_tiny(path):
    """Parse Tiny v2. Last 2 cols = official(obfuscated), intermediary.
       Returns list of (type, obf_key, int_val)."""
    entries = []
    curr_obf = ""
    curr_int = ""
    with open(path, "r", encoding="utf-8") as f:
        f.readline()  # skip header
        for line in f:
            line = line.strip()
            if not line: continue
            parts = line.split("\t")
            t = parts[0]
            n = len(parts)
            off = parts[n - 2]
            intr = parts[n - 1]
            if t == "c":
                curr_obf = off
                curr_int = intr
                entries.append(("c", off, intr))
            elif t == "m":
                desc = parts[1] if n >= 4 else ""
                entries.append(("m", f"{curr_obf}/{off}{desc}", f"{curr_int}/{intr}{desc}"))
            elif t == "f":
                desc = parts[1] if n >= 4 else ""
                entries.append(("f", f"{curr_obf}/{off}", f"{curr_int}/{intr}"))
    return entries


def build_tiny_mapping(tiny_path, pg_path, version_id, cache):
    """Build intermediary->official Tiny v2 for tiny-remapper."""
    entries = parse_tiny(tiny_path)
    pg = parse_pg_inverted(pg_path)
    print(f"[*] Tiny entries: {len(entries)}  ProGuard entries: {len(pg)}")

    # Map: interm_key -> (type, official_name)
    merged = {}
    for typ, obf_key, int_val in entries:
        off_val = None
        if obf_key in pg:
            off_val = pg[obf_key]
        elif "/" in obf_key:
            obf_cls, obf_member = obf_key.split("/", 1)
            if "(" in obf_member:
                lookup = f"{obf_cls}.{obf_member.split('(')[0]}"
            else:
                lookup = f"{obf_cls}.{obf_member}"
            if lookup in pg:
                off_val = pg[lookup]
            elif obf_member in pg:
                off_val = pg[obf_member]
        merged[int_val] = (typ, off_val if off_val else int_val)

    # Track obf->int class map
    obf_to_int_cls = {}
    for typ, obf_key, int_val in entries:
        if typ == "c": obf_to_int_cls[obf_key] = int_val

    # Write Tiny v2: intermediary -> official
    out = cache / f"mapping_{version_id}.tiny"
    with open(out, "w", encoding="utf-8") as f:
        f.write("tiny\t2\t0\tintermediary\tofficial\n")
        # Classes
        for typ, obf_key, int_val in entries:
            if typ != "c": continue
            _typ, off_val = merged[int_val]
            off_dot = off_val.replace("/", ".")
            f.write(f"c\t{int_val}\t{off_dot}\n")
        # Members
        for typ, obf_key, int_val in entries:
            if typ == "c": continue
            _typ, off_val = merged.get(int_val, (typ, int_val))
            parts = int_val.rsplit("/", 1)
            if len(parts) != 2: continue
            int_cls, int_mem = parts
            if "." in off_val:
                off_simple = off_val.rsplit(".", 1)[-1]
            elif "/" in off_val:
                off_simple = off_val.rsplit("/", 1)[-1]
            else:
                off_simple = off_val
            if typ == "m":
                desc_start = int_mem.find("(")
                if desc_start >= 0:
                    desc = int_mem[desc_start:]
                    method_name = int_mem[:desc_start]
                else:
                    desc = ""
                    method_name = int_mem
                f.write(f"m\t{desc}\t{int_cls}/{method_name}{desc}\t{off_simple}\n")
            elif typ == "f":
                f.write(f"f\t\t{int_cls}/{int_mem}\t{off_simple}\n")
    print(f"[+] Written Tiny v2 mapping: {out}")
    return out


# ---- Remap ----

def remap_jar(jar, mapping, client_jar, cache, out_dir):
    """Remap JAR with tiny-remapper, using client.jar on classpath."""
    tr = get_tr(cache)
    remapped = out_dir / f"{jar.stem}_remapped.jar"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["java", "-jar", str(tr), str(jar), str(remapped), str(mapping), "intermediary", "official"]
    if client_jar and client_jar.exists():
        cmd.append(str(client_jar))
        print(f"[*] Classpath: client.jar")
    print(f"[*] Remapping {jar.name} -> {remapped.name} ...")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[!] tiny-remapper failed:")
        print(proc.stderr[-2000:] if len(proc.stderr) > 2000 else proc.stderr)
        sys.exit(1)
    print(f"[+] Remapped: {remapped}")
    return remapped


# ---- Asset extraction ----

def extract_assets(source_jar, out_dir):
    """
    Extract non-class files and assets from the original or remapped JAR
    into the output directory alongside decompiled Java sources.
    """
    out_dir = Path(out_dir)
    exts = {".json", ".png", ".mcmeta", ".properties", ".lang", ".txt", ".toml",
            ".cfg", ".mixins", ".accesswidener", ".yml", ".yaml", ".nbt", ".snbt",
            ".zip", ".ogg", ".mp3", ".wav", ".fsh", ".vsh", ".glsl"}
    exclude_dirs = {"META-INF"}  # skip manifest/signatures
    count = 0

    print(f"[*] Extracting assets from {source_jar.name}...")
    with zipfile.ZipFile(source_jar, "r") as zf:
        for entry in zf.namelist():
            # Skip class files (handled by CFR) and META-INF
            parts = Path(entry).parts
            if parts and parts[0] in exclude_dirs:
                continue
            if entry.endswith(".class"):
                continue

            # Skip directory entries (they end with /)
            if entry.endswith("/"):
                continue

            # Check if it's an asset or mod resource
            ext = Path(entry).suffix.lower()
            if ext in exts or entry == "fabric.mod.json" or "assets/" in entry or "data/" in entry:
                dest = out_dir / entry
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Only write files, skip if a directory with this name already exists
                if not dest.exists() or dest.is_file():
                    if dest.exists():
                        dest.unlink()  # remove if CFR extracted an empty dir placeholder
                    with zf.open(entry) as src:
                        dest.write_bytes(src.read())
                    count += 1

    if count > 0:
        print(f"[+] Extracted {count} asset files")


# ---- Decompile ----

def decompile(orig_jar, out_dir, version_id, cache):
    cfr = get_cfr(cache)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if is_obf(version_id):
        print(f"[*] {version_id} is obfuscated, building mapping...")
        info = get_ver_info(version_id)
        pg = dl_mojang(version_id, info, cache)
        tiny = dl_inter(version_id, cache)
        client = dl_client(version_id, info, cache)
        if pg and tiny:
            mapping = build_tiny_mapping(tiny, pg, version_id, cache)
            remapped_jar = remap_jar(orig_jar, mapping, client, cache, out_dir)
            jar = remapped_jar
            # Extract assets from the remapped JAR (has correct class names)
            extract_assets(remapped_jar, out_dir)
        else:
            print("[!] Missing mappings, decompiling as-is")
            jar = orig_jar
            extract_assets(orig_jar, out_dir)
    else:
        print(f"[*] {version_id} is unobfuscated, decompiling directly")
        jar = orig_jar
        extract_assets(orig_jar, out_dir)

    cmd = ["java", "-jar", str(cfr), str(jar), "--outputdir", str(out_dir)]
    print(f"[*] Running CFR...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[!] CFR failed: {e}")
        sys.exit(1)

    print(f"[+] Done! Output: {out_dir}")


# ---- CLI ----

def main():
    p = argparse.ArgumentParser(description="Decompile a Minecraft Fabric mod JAR",
                                epilog="Supports Minecraft 1.20.0-26.2.")
    p.add_argument("jar", nargs="?", help="Fabric mod JAR")
    p.add_argument("-v", "--version", help="MC version")
    p.add_argument("-o", "--output", type=Path, help="Output dir")
    p.add_argument("--auto-detect", action="store_true", help="Detect version from fabric.mod.json")
    p.add_argument("--cache-dir", type=Path, help="Cache dir")
    p.add_argument("--list", action="store_true", help="List versions")
    p.add_argument("--force", action="store_true", help="Re-download assets")
    args = p.parse_args()

    if args.list:
        supported = sorted([v for v in all_releases() if is_sup(v)], key=lambda v: ver(v) or (0, 0, 0))
        print("Supported Minecraft release versions:")
        for v in supported: print(f"  {v} ({'obfuscated' if is_obf(v) else 'unobfuscated'})")
        return

    if not args.jar: p.error("jar required")
    jar = Path(args.jar)
    if not jar.exists(): print(f"[!] Not found: {jar}"); sys.exit(1)

    version = args.version
    if not version:
        version = mod_mc(jar)
        if version: print(f"[*] Detected version: {version}")
    if not version: print("[!] Use -v/--version"); sys.exit(1)
    if not is_sup(version): print(f"[!] {version} not supported"); sys.exit(1)

    c = _cache(args.cache_dir)
    if args.force:
        for pat in ["mojang_*", "inter_*", "mapping_*"]:
            for f in c.glob(pat): f.unlink()

    out = args.output or Path(f"decompiled_{jar.stem}")
    decompile(jar, out, version, c)


if __name__ == "__main__": main()
