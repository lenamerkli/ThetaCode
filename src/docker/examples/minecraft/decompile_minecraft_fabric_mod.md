# Decompile a Minecraft Fabric Mod
The `decompile_minecraft_fabric_mod` tool decompiles a Fabric mod JAR into readable Java source code for any Minecraft full release from **1.20.0** through **26.2**.

## Usage
```bash
~/software/decompile_minecraft_fabric_mod mod.jar
```

This detects the Minecraft version from the mod's `fabric.mod.json` and decompiles the JAR into a `decompiled_mod/` directory.

## Specifying the Version and Output
If the version cannot be auto-detected (or you want to override it), use `-v`/`--version`. Use `-o`/`--output` to choose the output directory:
```bash
~/software/decompile_minecraft_fabric_mod mod.jar -v 1.21.4 -o ./decompiled
```

## Auto-Detecting the Version
```bash
~/software/decompile_minecraft_fabric_mod mod.jar --auto-detect
```

## Listing Supported Versions
```bash
~/software/decompile_minecraft_fabric_mod --list
```

## Key Options
| Option          | Description                                                       |
|-----------------|-------------------------------------------------------------------|
| `jar`           | Path to the Fabric mod JAR                                        |
| `-v, --version` | Minecraft version (e.g. 1.21.4, 26.2)                             |
| `-o, --output`  | Output directory (default: `decompiled_<jar-name>`)               |
| `--auto-detect` | Detect the version from `fabric.mod.json`                         |
| `--cache-dir`   | Cache directory for downloaded mappings/CFR (default: `~/.cache`) |
| `--list`        | List all supported Minecraft versions                             |
| `--force`       | Re-download cached mappings                                       |

## Notes
- **1.x versions** (1.20.0 – 1.21.11) are obfuscated. The tool downloads Mojang mappings and Fabric Intermediary, remaps the JAR with tiny-remapper, then decompiles with CFR.
- **26.x versions** (26.1 – 26.2) are not obfuscated, so the JAR is decompiled directly with CFR.
- Requires `java` on PATH (for CFR and tiny-remapper).
- Non-class assets (JSON, PNG, textures, etc.) are extracted alongside the decompiled Java sources.
