# Avatar source acquisition

## Downloaded asset

- Source: https://github.com/PolygonalMind/100Avatars
- Asset: `100Avatars_001.zip`
- Local path: `100Avatars_001/100Avatars_001/`
- License stated by upstream release: CC0
- Contents: FBX + PNG texture + VRM + voxel assets

## Compatibility findings

- The VRM contains a humanoid definition with 52 bones.
- It contains VRM blend shapes for A/E/I/O/U and blink left/right, so it is a plausible starting point for lip sync.
- It is NOT a VRChat Unity Avatar project and does not contain VRC Avatar Descriptor, VRCExpressionParameters, VRCExpressionsMenu, FX Animator, or ActionID/Expression parameters.
- It cannot be converted to a `.vrca` until imported into Unity, configured with VRChat Avatars SDK 3, configured for PC, and uploaded through the user's VRChat account.

## Required next step

Install Unity Hub + a VRChat Creator Companion avatar project (matching the current VRChat SDK/Unity version), import the FBX/texture, configure Humanoid and Viseme lip sync, add Aether's `ActionID` and `Expression` Int parameters and Animator transitions, then upload. Upload requires the user's authenticated VRChat account and cannot be completed anonymously by this session.
