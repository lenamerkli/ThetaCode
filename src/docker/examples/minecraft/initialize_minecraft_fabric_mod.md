# Initialize a Minecraft Fabric Mod
The `initialize_minecraft_fabric_mod` tool creates a fully pre-configured  Fabric mod project for any Minecraft full release from **1.20.0** through  **26.2** — equivalent to what the *Minecraft Development* IntelliJ IDEA  plugin generates.

## Usage
```bash
~/software/initialize_minecraft_fabric_mod --version 1.21.4 \
    --name "My Mod" --modid mymod --package com.mymod \
    --output-dir ./my-mod
```

This creates a project at `./my-mod/mymod/` with:
- `build.gradle` — Gradle build script with Loom, mappings, and dependencies
- `gradle.properties` — Fabric version pins (loader, loom, fabric-api, minecraft)
- `settings.gradle` — Gradle project settings pointing to Fabric maven
- `gradlew` / `gradlew.bat` — Gradle wrapper scripts (pre-configured)
- `src/main/java/.../ExampleMod.java` — Main mod entrypoint
- `src/client/java/.../ExampleModClient.java` — Client-side entrypoint
- `src/main/java/.../mixin/ExampleMixin.java` — Example mixin
- `src/client/java/.../mixin/ExampleClientMixin.java` — Client mixin
- `src/main/resources/fabric.mod.json` — Mod metadata
- `.idea/runConfigurations/*.xml` — IntelliJ IDEA run configs

## Next Steps
After generating the project:

```bash
cd my-mod/mymod
./gradlew genSources    # generate deobfuscated Minecraft source code
./gradlew runClient     # launch Minecraft from the dev environment
./gradlew build         # build the mod .jar
```

## Deobfuscated Minecraft Source Code
Loom's `genSources` Gradle task decompiles the mapped Minecraft JAR. To extract the decompiled Java files to a folder on disk, use the `--extract-sources` flag:
```bash
python3 create_fabric_mod.py --version 1.21.4 --modid mymod --extract-sources
cd mymod
./gradlew genSources
# Extracts 4,500+ .java files into minecraft-sources/
```

## Listing Supported Versions
```bash
~/software/initialize_minecraft_fabric_mod --list-versions
```

## Key Options
| Option              | Description                                            |
|---------------------|--------------------------------------------------------|
| `--version`         | Minecraft full release (e.g. 1.21.4, 26.2)             |
| `--output-dir`      | Parent directory for the project (default: ".")        |
| `--name`            | Mod display name (default: "Example Mod")              |
| `--modid`           | Mod id, lowercase letters/numbers (default: "modid")   |
| `--package`         | Java package for classes (default: "com.example")      |
| `--author`          | Author name shown in fabric.mod.json (default: "Me!")  |
| `--fabric-api`      | Override the Fabric API version                        |
| `--extract-sources` | After genSources, unpack Minecraft .java files to disk |
| `--list-versions`   | List all supported Minecraft versions                  |

## Notes
- **1.x versions** (1.20.0 – 1.21.11) use official Mojang mappings.
- **26.x versions** (26.1 – 26.2) are not obfuscated, so no mappings are needed.
- The Java target is set automatically to match the Minecraft version (17, 21, or 25 depending on the version).
- Loom 1.17-SNAPSHOT requires Java ≥ 21 to run the Gradle daemon.
