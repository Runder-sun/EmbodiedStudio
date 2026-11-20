# Set up your own assets

Configure the [simulation host](compute.md) first. Asset sources for simulation live on that host. Paths below are relative to its configured `remoteRoot` unless stated otherwise. The source release includes the asset tools. Supply scene assets, robot models and optional policy weights from sources you are authorized to use.

## Import an asset for one project

| Asset | How to provide it |
| --- | --- |
| Public visual model | Give the generation request a direct HTTPS link to a self-contained GLB, its source page and license. Studio downloads it from the workbench and sends it to the compute host. This path prepares visual geometry. |
| Existing files on the compute host | Copy the USD or other native package into `assets/imports/<projectId>/`, keeping the original relative layout. Give the request the absolute entry path, dependency file list and source. The generator uses `assets_prepare` with `resourceFile` and `dependencyFiles`. |
| Asset authored during generation | The generator can author primitive scene geometry or prepare a reusable asset from project source using `build_asset(materials)`. |

For a native package, include the files the entry references, such as sublayers, meshes, textures, and supported material modules. Keep the entry and all explicit dependencies under one allowed library root. Studio prepares an immutable copy and returns a report reference. The generation tools use that reference when freezing the scene.

## Register a reusable asset library

Place your files and catalog on the simulation host. This example assumes a USD package with a single self-contained `chair.usda`. Use the axis, units, license and source for your actual asset.

```text
<remoteRoot>/
  assets/
    collections/
      catalog.json
      my-library/
        chair/
          chair.usda
```

Put this in `assets/collections/catalog.json`. If that catalog already has entries, append the new collection and asset to the existing arrays and keep their IDs unique.

```json
{
  "schema": "sceneagent-asset-collections-v1",
  "collections": [
    {
      "id": "my-library",
      "revision": "v1",
      "libraryTier": "preferred",
      "licenseEvidence": { "status": "unknown", "value": "unknown" }
    }
  ],
  "assets": [
    {
      "assetId": "collection:my-library:v1:chair",
      "collection": "my-library",
      "name": "office chair",
      "category": "chair",
      "format": "native_files",
      "sourceRoot": "current_assets",
      "packageRoot": "collections/my-library/chair",
      "entry": "chair.usda",
      "dependencyFiles": [],
      "upAxis": "Z",
      "units": "meters"
    }
  ]
}
```

- `packageRoot` is relative to the selected source root. `current_assets` uses `remoteRoot/assets`. For an existing read-only library, set `legacyRoot` and use `legacy_assets` or `legacy_data` for its `assets` or `data` subdirectory.
- `entry` and `dependencyFiles` are relative to `packageRoot`. Use ordinary files and normalized relative paths. Add the actual `sourcePage`, `sourceRevision` and `licenseEvidence` to the asset row when available. Leave the license unknown until it can be traced to the source.
- From the workstation, run `npm run scene -- assets "office chair"`. The lookup rebuilds the local index on the compute host as the catalog changes. In a generation request, name the asset ID or collection so the agent can find it and prepare it. Search verifies catalog registration. Scene build and robot execution verify their respective use of the asset.

## Add a robot model

Copy a real robot's complete USD or URDF model package into `assets/imports/robots/<projectId>/` or register it through your library. Keep the exact model variant, referenced meshes, textures, couplings and source information with the package. Tell the generator the robot model and source you want to use. The generator freezes its model resources and writes a project `*.robot.json` against the [robot configuration fields](../skills/scene-generation/references/api.md#机器人配置). A recorded rollout requires a compatible controller or policy and the actual frozen action and observation bindings.

See the [robot resource guide](../skills/scene-generation/references/robots.md) for upstream models. See the [asset API](../skills/scene-generation/references/api.md#资产与材料) for material maps, explicit MDL module roots, prerequisites for articulated objects, and prepared reports.
