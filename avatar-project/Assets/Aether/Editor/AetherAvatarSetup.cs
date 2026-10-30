// Aether Avatar 自动配置脚本（VRChat Avatars 3.0 规范版）
// 菜单 Aether → Setup Avatar 或批处理 -executeMethod Aether.AvatarSetup.RegenerateAll 一键完成：
//   1. Crimsom FBX 导入设置为 Humanoid
//   2. 场景中实例化 + VRC Avatar Descriptor
//   3. Viseme：优先精确绑定 VRChat 标准 15 视素（vrc.v_sil .. vrc.v_ou），杜绝误绑眨眼形态键
//   4. VRCExpressionParameters: ActionID(Int) + Expression(Int)，勾选 OSC/网络同步
//   5. 双控制器职责分离：
//      - AetherAction.controller → 挂载到 Playable Layer: Action（纯合法 HumanTrait 肌肉曲线 + VRCPlayableLayerControl + TrackingControl）
//      - AetherFX.controller     → 挂载到 Playable Layer: FX（纯面部非视素 BlendShape 曲线，零骨骼肌肉占用，不干扰口型同步）
//
// 依赖：VRChat Avatars SDK 3（VCC 工程自带）
#if UNITY_EDITOR && VRC_SDK_VRCSDK3
using System;
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEditor.Animations;
using UnityEditor.SceneManagement;
using UnityEngine;
using VRC.Core;
using VRC.SDK3.Avatars.Components;
using VRC.SDK3.Avatars.ScriptableObjects;

namespace Aether
{
    public static class AvatarSetup
    {
        const string FbxPath = "Assets/Aether/100Avatars_001_Crimsom.fbx";
        const string OutDir = "Assets/Aether/Generated";
        const string ActionControllerPath = OutDir + "/AetherAction.controller";
        const string FxControllerPath = OutDir + "/AetherFX.controller";
        const string ParamsPath = OutDir + "/AetherParameters.asset";
        const string MenuPath = OutDir + "/AetherMenu.asset";
        const string ScenePath = "Assets/Aether/AetherAvatar.unity";

        // 与 Aether config.yaml avatar.actions 严格一致
        static readonly (string name, int id, float duration, bool fullBody)[] Actions = {
            ("idle",       0, 0.0f, false),
            ("wave",       1, 1.8f, false),
            ("nod",        2, 1.0f, false),
            ("shake_head", 3, 1.2f, false),
            ("bow",        4, 2.0f, true),
            ("clap",       5, 1.5f, false),
            ("point",      6, 1.2f, false),
            ("think",      7, 2.5f, false),
            ("sit",        8, 2.0f, true),
            ("stand",      9, 2.0f, true),
        };

        // 与 Aether config.yaml avatar.expressions 严格一致
        static readonly (string name, int id)[] Expressions = {
            ("neutral", 0), ("smile", 1), ("surprised", 2), ("sad", 3), ("angry", 4),
        };

        // VRChat Avatars 3.0 官方 15 视素顺序（0..14）
        static readonly string[] StandardVrcVisemes = {
            "vrc.v_sil", "vrc.v_pp", "vrc.v_ff", "vrc.v_th", "vrc.v_dd",
            "vrc.v_kk",  "vrc.v_ch", "vrc.v_ss", "vrc.v_nn", "vrc.v_rr",
            "vrc.v_aa",  "vrc.v_ee", "vrc.v_ih", "vrc.v_oh", "vrc.v_ou",
        };

        [MenuItem("Aether/Setup Avatar")]
        public static void Run()
        {
            Directory.CreateDirectory(OutDir);

            if (File.Exists(ScenePath))
            {
                var activeScene = EditorSceneManager.GetActiveScene();
                if (activeScene.path != ScenePath)
                    EditorSceneManager.OpenScene(ScenePath);
            }

            // ── 1. FBX → Humanoid ────────────────────────────
            var importer = (ModelImporter)AssetImporter.GetAtPath(FbxPath);
            if (importer == null)
            {
                var msg = $"未找到 {FbxPath}，先把 FBX 拷到该路径。";
                if (!Application.isBatchMode)
                    EditorUtility.DisplayDialog("Aether", msg, "OK");
                throw new FileNotFoundException(msg, FbxPath);
            }
            if (importer.animationType != ModelImporterAnimationType.Human)
            {
                importer.animationType = ModelImporterAnimationType.Human;
                importer.autoGenerateAvatarMappingIfUnspecified = true;
                importer.SaveAndReimport();
            }

            // ── 2. 实例化 + Descriptor ───────────────────────
            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(FbxPath);
            var instance = UnityEngine.Object.FindObjectOfType<VRCAvatarDescriptor>()?.gameObject;
            if (instance == null)
            {
                instance = (GameObject)PrefabUtility.InstantiatePrefab(prefab);
                instance.name = "AetherAvatar";
            }
            var descriptor = instance.GetComponent<VRCAvatarDescriptor>()
                          ?? instance.AddComponent<VRCAvatarDescriptor>();
            if (instance.GetComponent<PipelineManager>() == null)
                instance.AddComponent<PipelineManager>();

            var animator = instance.GetComponent<Animator>();
            var head = animator != null ? animator.GetBoneTransform(HumanBodyBones.Head) : null;
            if (head != null)
                descriptor.ViewPosition = instance.transform.InverseTransformPoint(
                    head.position + new Vector3(0f, 0.06f, 0.05f));

            // ── 3. Viseme 精确绑定 ───────────────────────────
            ConfigureVisemes(descriptor, instance);

            // ── 4. Expression Parameters（保留已有 GUID）────
            var parameters = EnsureExpressionParameters();
            var menu = EnsureExpressionsMenu();
            descriptor.customExpressions = true;
            descriptor.expressionParameters = parameters;
            descriptor.expressionsMenu = menu;

            // ── 5. Action + FX 双控制器构建与挂载 ───────────
            var actionController = BuildActionController(instance);
            var fxController = BuildFxController(instance);

            descriptor.customizeAnimationLayers = true;
            EnsureBaseAnimationLayersInitialized(descriptor);
            for (int i = 0; i < descriptor.baseAnimationLayers.Length; i++)
            {
                var layerType = descriptor.baseAnimationLayers[i].type;
                if (layerType == VRCAvatarDescriptor.AnimLayerType.Action)
                {
                    descriptor.baseAnimationLayers[i].isEnabled = true;
                    descriptor.baseAnimationLayers[i].isDefault = false;
                    descriptor.baseAnimationLayers[i].animatorController = actionController;
                }
                else if (layerType == VRCAvatarDescriptor.AnimLayerType.FX)
                {
                    descriptor.baseAnimationLayers[i].isEnabled = true;
                    descriptor.baseAnimationLayers[i].isDefault = false;
                    descriptor.baseAnimationLayers[i].animatorController = fxController;
                }
            }

            EditorUtility.SetDirty(descriptor);
            EditorSceneManager.MarkSceneDirty(instance.scene);
            EditorSceneManager.SaveScene(instance.scene, ScenePath);
            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();
            Debug.Log("[Aether] Avatar 配置完成：Humanoid + 15 Visemes + 参数 + Action/FX 双层 + 场景");
        }

        // 批处理无头入口：
        // Unity.exe -batchmode -nographics -quit -projectPath avatar-project -executeMethod Aether.AvatarSetup.RegenerateAll
        [MenuItem("Aether/Regenerate All")]
        public static void RegenerateAll()
        {
            Run();
        }

        [MenuItem("Aether/Regenerate Controllers Only")]
        public static void GenerateFxController()
        {
            Directory.CreateDirectory(OutDir);
            if (File.Exists(ScenePath))
                EditorSceneManager.OpenScene(ScenePath);
            var instance = UnityEngine.Object.FindObjectOfType<VRCAvatarDescriptor>()?.gameObject;
            BuildActionController(instance);
            BuildFxController(instance);
            AssetDatabase.SaveAssets();
            Debug.Log("[Aether] AetherAction.controller & AetherFX.controller regenerated");
        }

        [MenuItem("Aether/Build And Test")]
        public static void BuildAndTest()
        {
            if (!File.Exists(ScenePath))
                Run();

            EditorSceneManager.OpenScene(ScenePath);
            var descriptor = UnityEngine.Object.FindObjectOfType<VRCAvatarDescriptor>();
            if (descriptor == null)
                throw new MissingComponentException($"{ScenePath} 中没有 VRCAvatarDescriptor");

            VRC.SDKBase.Editor.VRC_SdkBuilder
                .RunExportAndTestAvatarBlueprint(descriptor.gameObject);
            Debug.Log("[Aether] Avatar Build & Test 已启动");
        }

        // ══════════════════════════════════════════════════════
        // 参数与层初始化辅助
        // ══════════════════════════════════════════════════════

        static VRCExpressionParameters EnsureExpressionParameters()
        {
            var parameters = AssetDatabase.LoadAssetAtPath<VRCExpressionParameters>(ParamsPath);
            if (parameters == null)
            {
                parameters = ScriptableObject.CreateInstance<VRCExpressionParameters>();
                AssetDatabase.CreateAsset(parameters, ParamsPath);
            }
            parameters.parameters = new[]
            {
                new VRCExpressionParameters.Parameter
                {
                    name = "ActionID",
                    valueType = VRCExpressionParameters.ValueType.Int,
                    defaultValue = 0f,
                    saved = false,
                    networkSynced = true,
                },
                new VRCExpressionParameters.Parameter
                {
                    name = "Expression",
                    valueType = VRCExpressionParameters.ValueType.Int,
                    defaultValue = 0f,
                    saved = false,
                    networkSynced = true,
                },
            };
            EditorUtility.SetDirty(parameters);
            return parameters;
        }

        static VRCExpressionsMenu EnsureExpressionsMenu()
        {
            var menu = AssetDatabase.LoadAssetAtPath<VRCExpressionsMenu>(MenuPath);
            if (menu == null)
            {
                menu = ScriptableObject.CreateInstance<VRCExpressionsMenu>();
                AssetDatabase.CreateAsset(menu, MenuPath);
            }
            EditorUtility.SetDirty(menu);
            return menu;
        }

        static void EnsureBaseAnimationLayersInitialized(VRCAvatarDescriptor descriptor)
        {
            if (descriptor.baseAnimationLayers != null && descriptor.baseAnimationLayers.Length == 5)
                return;

            var types = new[]
            {
                VRCAvatarDescriptor.AnimLayerType.Base,
                VRCAvatarDescriptor.AnimLayerType.Additive,
                VRCAvatarDescriptor.AnimLayerType.Gesture,
                VRCAvatarDescriptor.AnimLayerType.Action,
                VRCAvatarDescriptor.AnimLayerType.FX,
            };
            var layers = new VRCAvatarDescriptor.CustomAnimLayer[types.Length];
            for (int t = 0; t < types.Length; t++)
            {
                layers[t] = new VRCAvatarDescriptor.CustomAnimLayer
                {
                    type = types[t],
                    isDefault = true,
                    isEnabled = true,
                };
            }
            descriptor.baseAnimationLayers = layers;
        }

        // ══════════════════════════════════════════════════════
        // Viseme 精确映射（杜绝 Contains("e") 误中 blink_left）
        // ══════════════════════════════════════════════════════

        static void ConfigureVisemes(VRCAvatarDescriptor descriptor, GameObject root)
        {
            var renderer = FindFaceMeshRenderer(root);
            if (renderer == null || renderer.sharedMesh == null)
                return;

            var mesh = renderer.sharedMesh;
            var available = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (int bs = 0; bs < mesh.blendShapeCount; bs++)
            {
                var shapeName = mesh.GetBlendShapeName(bs);
                available[shapeName] = shapeName;
            }

            if (!string.IsNullOrEmpty(descriptor.MouthOpenBlendShapeName) &&
                !available.ContainsKey(descriptor.MouthOpenBlendShapeName))
            {
                descriptor.MouthOpenBlendShapeName = "";
            }

            // 优先匹配 VRChat 官方 15 视素标准集合（Crimsom FBX 15 个全齐）
            if (available.ContainsKey("vrc.v_aa") && available.ContainsKey("vrc.v_ee"))
            {
                descriptor.lipSync = VRC.SDKBase.VRC_AvatarDescriptor.LipSyncStyle.VisemeBlendShape;
                descriptor.VisemeSkinnedMesh = renderer;
                descriptor.VisemeBlendShapes = new string[15];
                for (int v = 0; v < 15; v++)
                {
                    if (available.TryGetValue(StandardVrcVisemes[v], out var exact))
                        descriptor.VisemeBlendShapes[v] = exact;
                    else
                        throw new InvalidOperationException(
                            $"网格 {mesh.name} 缺少标准视素形态键: {StandardVrcVisemes[v]}");
                }
                return;
            }

            // 兜底：VRM 五元音精确词匹配（严禁裸子串匹配，排除 blink / lid）
            string FindExactVowel(params string[] candidates)
            {
                foreach (var candidate in candidates)
                    if (available.TryGetValue(candidate, out var hit))
                        return hit;
                return null;
            }

            string a = FindExactVowel("Fcl_MTH_A", "MTH_A", "Vowel_A", "A", "aa");
            string e = FindExactVowel("Fcl_MTH_E", "MTH_E", "Vowel_E", "E", "ee");
            string i = FindExactVowel("Fcl_MTH_I", "MTH_I", "Vowel_I", "I", "ih");
            string o = FindExactVowel("Fcl_MTH_O", "MTH_O", "Vowel_O", "O", "oh");
            string u = FindExactVowel("Fcl_MTH_U", "MTH_U", "Vowel_U", "U", "ou");

            if (a == null)
            {
                descriptor.lipSync = VRC.SDKBase.VRC_AvatarDescriptor.LipSyncStyle.Default;
                return;
            }

            descriptor.lipSync = VRC.SDKBase.VRC_AvatarDescriptor.LipSyncStyle.VisemeBlendShape;
            descriptor.VisemeSkinnedMesh = renderer;
            descriptor.VisemeBlendShapes = new[]
            {
                null, u, u, i, o, e, e, u, a, e, a, e, i, o, u,
            };
        }

        static SkinnedMeshRenderer FindFaceMeshRenderer(GameObject root)
        {
            if (root == null)
                return null;
            var renderers = root.GetComponentsInChildren<SkinnedMeshRenderer>(true);
            SkinnedMeshRenderer best = null;
            int bestCount = -1;
            foreach (var r in renderers)
            {
                int count = r.sharedMesh != null ? r.sharedMesh.blendShapeCount : 0;
                if (count > bestCount)
                {
                    best = r;
                    bestCount = count;
                }
            }
            return best;
        }

        // ══════════════════════════════════════════════════════
        // 1. Action Controller（骨骼肌肉层，挂载到 AnimLayerType.Action）
        // ══════════════════════════════════════════════════════

        static AnimatorController BuildActionController(GameObject avatarRoot)
        {
            var validProps = CollectValidAnimatorMuscleProperties(avatarRoot);
            var controller = RecreateEmptyController(ActionControllerPath);
            controller.AddParameter("ActionID", AnimatorControllerParameterType.Int);

            var machine = CreateLayerStateMachine(controller, "AetherActions", defaultWeight: 1f);
            var idle = machine.AddState("idle");
            idle.writeDefaultValues = true;
            machine.defaultState = idle;

            // VRChat Action Playable Layer 默认权重为 0：
            // idle 将 Action 层权重置 0 并把追踪交还给 SteamVR 6-DoF 追踪器；
            // 激活态将 Action 层权重拉到 1 并切换对应部位到 Animation。
            AddActionLayerWeightControl(idle, goalWeight: 0f, blendDuration: 0.15f);
            var idleTracking = idle.AddStateMachineBehaviour<VRCAnimatorTrackingControl>();
            SetTracking(idleTracking, fullBody: true, idle: true);

            foreach (var (name, id, duration, fullBody) in Actions)
            {
                if (id == 0)
                    continue;

                var state = machine.AddState(name);
                state.writeDefaultValues = true;
                var clip = BuildActionMuscleClip(name, duration, validProps);
                VerifyClipHasBindings(clip);
                AssetDatabase.AddObjectToAsset(clip, controller);
                state.motion = clip;

                AddActionLayerWeightControl(state, goalWeight: 1f, blendDuration: 0.12f);
                var tracking = state.AddStateMachineBehaviour<VRCAnimatorTrackingControl>();
                SetTracking(tracking, fullBody: fullBody, idle: false);

                var to = idle.AddTransition(state);
                to.AddCondition(AnimatorConditionMode.Equals, id, "ActionID");
                to.hasExitTime = false;
                to.duration = 0.12f;

                var back = state.AddTransition(idle);
                back.AddCondition(AnimatorConditionMode.NotEqual, id, "ActionID");
                back.hasExitTime = false;
                back.duration = 0.18f;
            }

            EditorUtility.SetDirty(controller);
            return controller;
        }

        static HashSet<string> CollectValidAnimatorMuscleProperties(GameObject avatarRoot)
        {
            var valid = new HashSet<string>(HumanTrait.MuscleName, StringComparer.Ordinal);
            if (avatarRoot != null)
            {
                foreach (var binding in AnimationUtility.GetAnimatableBindings(avatarRoot, avatarRoot))
                {
                    if (binding.type == typeof(Animator) && string.IsNullOrEmpty(binding.path))
                        valid.Add(binding.propertyName);
                }
            }
            return valid;
        }

        static AnimationClip BuildActionMuscleClip(
            string actionName, float duration, HashSet<string> validProps)
        {
            var clip = new AnimationClip
            {
                name = $"{actionName}_action",
                frameRate = 30f,
            };
            SetLoopTime(clip, loop: false);

            switch (actionName)
            {
                case "wave":
                    SetMuscleCurve(clip, validProps, "Right Shoulder Down-Up",
                        HoldCurve(duration, 0.25f, 1.55f, 0.35f));
                    SetMuscleCurve(clip, validProps, "Right Arm Down-Up",
                        HoldCurve(duration, 0.25f, 1.55f, 0.75f));
                    SetMuscleCurve(clip, validProps, "Right Forearm Stretch",
                        HoldCurve(duration, 0.25f, 1.55f, -0.35f));
                    SetMuscleCurve(clip, validProps, "Right Arm Twist In-Out", new AnimationCurve(
                        new Keyframe(0.00f,  0.00f),
                        new Keyframe(0.45f,  0.50f),
                        new Keyframe(0.90f, -0.50f),
                        new Keyframe(1.35f,  0.50f),
                        new Keyframe(duration, 0.00f)));
                    break;

                case "nod":
                    var nodCurve = new AnimationCurve(
                        new Keyframe(0.00f,  0.00f),
                        new Keyframe(0.25f, -0.45f),
                        new Keyframe(0.50f,  0.15f),
                        new Keyframe(0.75f, -0.40f),
                        new Keyframe(duration, 0.00f));
                    SetMuscleCurve(clip, validProps, "Neck Nod Down-Up", nodCurve);
                    SetMuscleCurve(clip, validProps, "Head Nod Down-Up", nodCurve);
                    break;

                case "shake_head":
                    var shakeCurve = new AnimationCurve(
                        new Keyframe(0.00f,  0.00f),
                        new Keyframe(0.25f, -0.50f),
                        new Keyframe(0.60f,  0.50f),
                        new Keyframe(0.95f, -0.45f),
                        new Keyframe(duration, 0.00f));
                    SetMuscleCurve(clip, validProps, "Neck Turn Left-Right", shakeCurve);
                    SetMuscleCurve(clip, validProps, "Head Turn Left-Right", shakeCurve);
                    break;

                case "bow":
                    SetMuscleCurve(clip, validProps, "Spine Front-Back",
                        HoldCurve(duration, 0.50f, 1.40f, -0.65f));
                    SetMuscleCurve(clip, validProps, "Chest Front-Back",
                        HoldCurve(duration, 0.50f, 1.40f, -0.45f));
                    SetMuscleCurve(clip, validProps, "Neck Nod Down-Up",
                        HoldCurve(duration, 0.50f, 1.40f, -0.30f));
                    break;

                case "clap":
                    SetMuscleCurve(clip, validProps, "Left Arm Front-Back",
                        HoldCurve(duration, 0.25f, 1.25f, 0.65f));
                    SetMuscleCurve(clip, validProps, "Right Arm Front-Back",
                        HoldCurve(duration, 0.25f, 1.25f, 0.65f));
                    var clapForearm = new AnimationCurve(
                        new Keyframe(0.00f,  0.00f),
                        new Keyframe(0.35f, -0.55f),
                        new Keyframe(0.60f, -0.25f),
                        new Keyframe(0.85f, -0.55f),
                        new Keyframe(1.10f, -0.25f),
                        new Keyframe(duration, 0.00f));
                    SetMuscleCurve(clip, validProps, "Left Forearm Stretch", clapForearm);
                    SetMuscleCurve(clip, validProps, "Right Forearm Stretch", clapForearm);
                    break;

                case "point":
                    SetMuscleCurve(clip, validProps, "Right Arm Front-Back",
                        HoldCurve(duration, 0.25f, 0.95f, 0.80f));
                    SetMuscleCurve(clip, validProps, "Right Arm Down-Up",
                        HoldCurve(duration, 0.25f, 0.95f, 0.25f));
                    SetMuscleCurve(clip, validProps, "Right Forearm Stretch",
                        HoldCurve(duration, 0.25f, 0.95f, 0.75f));
                    if (validProps.Contains("RightHand.Index.1 Stretched"))
                    {
                        SetMuscleCurve(clip, validProps, "RightHand.Index.1 Stretched",
                            HoldCurve(duration, 0.20f, 0.95f, 0.70f));
                        SetMuscleCurve(clip, validProps, "RightHand.Middle.1 Stretched",
                            HoldCurve(duration, 0.20f, 0.95f, -0.75f));
                        SetMuscleCurve(clip, validProps, "RightHand.Ring.1 Stretched",
                            HoldCurve(duration, 0.20f, 0.95f, -0.75f));
                        SetMuscleCurve(clip, validProps, "RightHand.Little.1 Stretched",
                            HoldCurve(duration, 0.20f, 0.95f, -0.75f));
                    }
                    break;

                case "think":
                    SetMuscleCurve(clip, validProps, "Right Arm Front-Back",
                        HoldCurve(duration, 0.40f, 2.10f, 0.55f));
                    SetMuscleCurve(clip, validProps, "Right Forearm Stretch",
                        HoldCurve(duration, 0.40f, 2.10f, -0.80f));
                    SetMuscleCurve(clip, validProps, "Head Tilt Left-Right",
                        HoldCurve(duration, 0.40f, 2.10f, 0.35f));
                    break;

                case "sit":
                    // 坐姿保持在末帧，由 ActionID != 8 过渡平缓回到站立
                    SetMuscleCurve(clip, validProps, "Spine Front-Back",
                        SustainCurve(duration, 0.60f, -0.15f));
                    SetMuscleCurve(clip, validProps, "Left Upper Leg Front-Back",
                        SustainCurve(duration, 0.60f, 0.85f));
                    SetMuscleCurve(clip, validProps, "Right Upper Leg Front-Back",
                        SustainCurve(duration, 0.60f, 0.85f));
                    SetMuscleCurve(clip, validProps, "Left Lower Leg Stretch",
                        SustainCurve(duration, 0.60f, -0.80f));
                    SetMuscleCurve(clip, validProps, "Right Lower Leg Stretch",
                        SustainCurve(duration, 0.60f, -0.80f));
                    break;

                case "stand":
                    SetMuscleCurve(clip, validProps, "Spine Front-Back",
                        SettleToZeroCurve(duration, -0.10f, 0.60f));
                    SetMuscleCurve(clip, validProps, "Left Upper Leg Front-Back",
                        SettleToZeroCurve(duration, 0.15f, 0.60f));
                    SetMuscleCurve(clip, validProps, "Right Upper Leg Front-Back",
                        SettleToZeroCurve(duration, 0.15f, 0.60f));
                    SetMuscleCurve(clip, validProps, "Left Lower Leg Stretch",
                        SettleToZeroCurve(duration, -0.15f, 0.60f));
                    SetMuscleCurve(clip, validProps, "Right Lower Leg Stretch",
                        SettleToZeroCurve(duration, -0.15f, 0.60f));
                    break;

                default:
                    throw new ArgumentOutOfRangeException(nameof(actionName), $"未定义的预录动作: {actionName}");
            }

            return clip;
        }

        static void SetMuscleCurve(
            AnimationClip clip, HashSet<string> validProps, string muscle, AnimationCurve curve)
        {
            if (!validProps.Contains(muscle))
            {
                throw new InvalidOperationException(
                    $"[Aether] 非法 Humanoid 肌肉属性名 '{muscle}'（不在 HumanTrait.MuscleName / Animator 可动画绑定中）");
            }
            var binding = EditorCurveBinding.FloatCurve("", typeof(Animator), muscle);
            AnimationUtility.SetEditorCurve(clip, binding, curve);
        }

        static void AddActionLayerWeightControl(
            AnimatorState state, float goalWeight, float blendDuration)
        {
            var ctrl = state.AddStateMachineBehaviour<VRCPlayableLayerControl>();
            ctrl.layer = VRC.SDKBase.VRC_PlayableLayerControl.BlendableLayer.Action;
            ctrl.goalWeight = goalWeight;
            ctrl.blendDuration = blendDuration;
        }

        // ══════════════════════════════════════════════════════
        // 2. FX Controller（纯面部表情层，挂载到 AnimLayerType.FX）
        // ══════════════════════════════════════════════════════

        static AnimatorController BuildFxController(GameObject avatarRoot)
        {
            var renderer = FindFaceMeshRenderer(avatarRoot);
            string meshPath = "mesh_char_001";
            var availableShapes = new HashSet<string>(StringComparer.Ordinal);
            if (renderer != null && avatarRoot != null)
            {
                meshPath = AnimationUtility.CalculateTransformPath(renderer.transform, avatarRoot.transform);
                if (renderer.sharedMesh != null)
                {
                    for (int i = 0; i < renderer.sharedMesh.blendShapeCount; i++)
                        availableShapes.Add(renderer.sharedMesh.GetBlendShapeName(i));
                }
            }
            else
            {
                availableShapes.UnionWith(new[]
                {
                    "vrc.blink_left", "vrc.blink_right",
                    "vrc.lowerlid_left", "vrc.lowerlid_right",
                });
            }

            var controller = RecreateEmptyController(FxControllerPath);
            controller.AddParameter("Expression", AnimatorControllerParameterType.Int);

            var machine = CreateLayerStateMachine(controller, "AetherExpressions", defaultWeight: 1f);
            // neutral 保持空 Motion + writeDefaultValues=true，切回 neutral 时自动归零表情形态键且不锁死眨眼/口型
            var neutral = machine.AddState("neutral");
            neutral.writeDefaultValues = true;
            machine.defaultState = neutral;

            foreach (var (exprName, id) in Expressions)
            {
                if (id == 0)
                    continue;

                var state = machine.AddState(exprName);
                state.writeDefaultValues = true;
                var clip = BuildFacialExpressionClip(exprName, meshPath, availableShapes);
                VerifyClipHasBindings(clip);
                AssetDatabase.AddObjectToAsset(clip, controller);
                state.motion = clip;

                var to = neutral.AddTransition(state);
                to.AddCondition(AnimatorConditionMode.Equals, id, "Expression");
                to.hasExitTime = false;
                to.duration = 0.12f;

                var back = state.AddTransition(neutral);
                back.AddCondition(AnimatorConditionMode.NotEqual, id, "Expression");
                back.hasExitTime = false;
                back.duration = 0.15f;
            }

            EditorUtility.SetDirty(controller);
            return controller;
        }

        static AnimationClip BuildFacialExpressionClip(
            string exprName, string meshPath, HashSet<string> availableShapes)
        {
            var clip = new AnimationClip
            {
                name = $"{exprName}_face",
                frameRate = 30f,
            };
            SetLoopTime(clip, loop: false);

            // 严禁在 FX 表情剪辑中驱动 vrc.v_* 视素键，否则会覆盖 VRChat 说话时的实时 LipSync。
            float lowerLid = 0f;
            float blink = 0f;
            switch (exprName)
            {
                case "smile":
                    lowerLid = 75f;
                    blink = 20f;
                    break;
                case "surprised":
                    lowerLid = 0f;
                    blink = 0f;
                    break;
                case "sad":
                    lowerLid = 15f;
                    blink = 55f;
                    break;
                case "angry":
                    lowerLid = 60f;
                    blink = 35f;
                    break;
                default:
                    throw new ArgumentOutOfRangeException(nameof(exprName), $"未定义的表情: {exprName}");
            }

            SetBlendShapeConstant(clip, meshPath, availableShapes, "vrc.lowerlid_left", lowerLid);
            SetBlendShapeConstant(clip, meshPath, availableShapes, "vrc.lowerlid_right", lowerLid);
            SetBlendShapeConstant(clip, meshPath, availableShapes, "vrc.blink_left", blink);
            SetBlendShapeConstant(clip, meshPath, availableShapes, "vrc.blink_right", blink);

            return clip;
        }

        static void SetBlendShapeConstant(
            AnimationClip clip, string meshPath, HashSet<string> availableShapes,
            string shapeName, float weight)
        {
            if (!availableShapes.Contains(shapeName))
                throw new InvalidOperationException(
                    $"[Aether] 面部网格 '{meshPath}' 不存在 BlendShape '{shapeName}'");

            if (shapeName.StartsWith("vrc.v_", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    $"[Aether] FX 表情层禁止绑定口型视素 '{shapeName}'，否则会锁死语音口型同步");

            var curve = new AnimationCurve(
                new Keyframe(0.0f, weight),
                new Keyframe(0.2f, weight));
            var binding = EditorCurveBinding.FloatCurve(
                meshPath, typeof(SkinnedMeshRenderer), "blendShape." + shapeName);
            AnimationUtility.SetEditorCurve(clip, binding, curve);
        }

        // ══════════════════════════════════════════════════════
        // 控制器与曲线通用工具
        // ══════════════════════════════════════════════════════

        static AnimatorController RecreateEmptyController(string assetPath)
        {
            AssetDatabase.DeleteAsset(assetPath);
            var controller = AnimatorController.CreateAnimatorControllerAtPath(assetPath);
            // CreateAnimatorControllerAtPath 默认会创建一个空的 "Base Layer"，先清理掉避免权重空转
            while (controller.layers.Length > 0)
                controller.RemoveLayer(0);
            return controller;
        }

        static AnimatorStateMachine CreateLayerStateMachine(
            AnimatorController controller, string layerName, float defaultWeight)
        {
            var machine = new AnimatorStateMachine
            {
                name = layerName,
                hideFlags = HideFlags.HideInHierarchy,
            };
            AssetDatabase.AddObjectToAsset(machine, controller);
            controller.AddLayer(new AnimatorControllerLayer
            {
                name = layerName,
                stateMachine = machine,
                defaultWeight = defaultWeight,
            });
            return machine;
        }

        static void SetLoopTime(AnimationClip clip, bool loop)
        {
            var settings = AnimationUtility.GetAnimationClipSettings(clip);
            settings.loopTime = loop;
            AnimationUtility.SetAnimationClipSettings(clip, settings);
        }

        static void VerifyClipHasBindings(AnimationClip clip)
        {
            var bindings = AnimationUtility.GetCurveBindings(clip);
            if (bindings == null || bindings.Length == 0)
                throw new InvalidOperationException(
                    $"[Aether] 动画剪辑 '{clip.name}' 未生成任何有效曲线绑定");
        }

        static AnimationCurve HoldCurve(float duration, float easeInEnd, float holdEnd, float peak)
        {
            return new AnimationCurve(
                new Keyframe(0f, 0f),
                new Keyframe(easeInEnd, peak),
                new Keyframe(holdEnd, peak),
                new Keyframe(duration, 0f));
        }

        static AnimationCurve SustainCurve(float duration, float easeInEnd, float target)
        {
            return new AnimationCurve(
                new Keyframe(0f, 0f),
                new Keyframe(easeInEnd, target),
                new Keyframe(duration, target));
        }

        static AnimationCurve SettleToZeroCurve(float duration, float initialOffset, float settleAt)
        {
            return new AnimationCurve(
                new Keyframe(0f, initialOffset),
                new Keyframe(settleAt, 0f),
                new Keyframe(duration, 0f));
        }

        static void SetTracking(
            VRCAnimatorTrackingControl tracking, bool fullBody, bool idle)
        {
            var type = idle
                ? VRC.SDKBase.VRC_AnimatorTrackingControl.TrackingType.Tracking
                : VRC.SDKBase.VRC_AnimatorTrackingControl.TrackingType.Animation;
            var noChange = VRC.SDKBase.VRC_AnimatorTrackingControl.TrackingType.NoChange;

            tracking.trackingHead = fullBody ? type : noChange;
            tracking.trackingLeftHand = type;
            tracking.trackingRightHand = type;
            tracking.trackingHip = fullBody ? type : noChange;
            tracking.trackingLeftFoot = fullBody ? type : noChange;
            tracking.trackingRightFoot = fullBody ? type : noChange;
            tracking.trackingLeftFingers = type;
            tracking.trackingRightFingers = type;
        }
    }
}
#endif
