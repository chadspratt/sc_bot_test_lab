# Bot Build Configs

This directory contains build-specific configuration overrides for custom bots.

## Structure

```
configs/
  <bot_directory_name>/
    <build_name>/
      <config_files...>
```

Each bot has a folder matching its `bot_directory` name (the folder name under `aiarena/bots/`).
Inside that folder, each subfolder represents a named build. The contents of a build folder
are config files that will be **copied into the bot's root directory** before a match starts,
overriding the default files.

## Example: `who` bot

The `who` bot uses three YAML files (`protoss_builds.yml`, `terran_builds.yml`, `zerg_builds.yml`)
to select its build order. By providing modified versions of these files in a build config folder,
you can force the bot to use a specific build.

```
configs/
  who/
    ProxyNexus/
      protoss_builds.yml    ← forces ProxyNexus build vs Protoss
      terran_builds.yml     ← forces ProxyNexus build vs Terran
      zerg_builds.yml       ← forces ProxyNexus build vs Zerg
    GatewayAllIn/
      protoss_builds.yml
      terran_builds.yml
      zerg_builds.yml
```

## How it works

When a match is started with a specific build selected:

1. The config files from the build folder are mounted as Docker volume overlays
   on top of the bot's directory, overriding the default files.
2. The build name is recorded on the match record (`friendly_build` for the test bot,
   `opponent_build` for opponent bots).

## Adding builds for a new bot

1. Create a folder under `configs/` matching the bot's directory name in `aiarena/bots/`.
2. Inside that folder, create a subfolder for each build you want to define.
3. Place the config files that need to be overridden inside each build folder.
4. The builds will automatically appear in the UI dropdowns.

## Notes

- Build config files are applied as Docker overlay mounts, so the original bot files
  are not modified on disk.
- The "None" option in the UI means no build override — the bot runs with its default config.
- This system is bot-specific; the config files and their format depend on how each bot
  selects its build order.
