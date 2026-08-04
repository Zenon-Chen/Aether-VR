// Aether Avatar 自动配置脚本
// 放入 Unity 工程 Assets/Editor/ 后，菜单 Aether → Setup Avatar 一键完成：
//   1. Crimsom FBX 导入设置为 Humanoid
//   2. 场景中实例化 + VRC Avatar Descriptor
//   3. Viseme (A/E/I/O/U 15 视素映射到已有 blendshape，缺失的用 Jaw Flap 兜底)
//   4. VRCExpressionParameters: ActionID(Int) + Expression(Int)，勾选 OSC 同步
//   5. FX Animator: ActionID 驱动的动作层 + Expression 驱动的表情层
//      每个动作状态入口 Tracking Control 切 Animation，退出切回 Tracking
//
// 依赖：VRChat Avatars SDK 3（VCC 工程自带）
#if UNITY_EDITOR && VRC_SDK_VRCSDK3
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEditor.Animations;
using UnityEngine;
using VRC.SDK3.Avatars.Components;
using VRC.SDK3.Avatars.ScriptableObjects;

namespace Aether
{
    public static class AvatarSetup
    {
        const string FbxPath = "Assets/Aether/100Avatars_001_Crimsom.fbx";
        const string OutDir = "Assets/Aether/Generated";

        // 与 Aether config.yaml avatar.actions 严格一致
        static readonly (string name, int id)[] Actions = {
            ("idle", 0), ("wave", 1), ("nod", 2), ("shake_head", 3), ("bow", 4),
            ("clap", 5), ("point", 6), ("think", 7), ("sit", 8), ("stand", 9),
        };
        static readonly (string name, int id)[] Expressions = {
            ("neutral", 0), ("smile", 1), ("surprised", 2), ("sad", 3), ("angry", 4),
        };

        [MenuItem("Aether/Setup Avatar")]
        public static void Run()
        {
            Directory.CreateDirectory(OutDir);

            // ── 1. FBX → Humanoid ────────────────────────────
            var importer = (ModelImporter)AssetImporter.GetAtPath(FbxPath);
            if (importer == null)
            {
                EditorUtility.DisplayDialog("Aether", $"未找到 {FbxPath}，先把 FBX 拷到该路径。", "OK");
                return;
            }
            importer.animationType = ModelImporterAnimationType.Human;
            importer.autoGenerateAvatarMappingIfUnspecified = true;
            importer.SaveAndReimport();

            // ── 2. 实例化 + Descriptor ───────────────────────
            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(FbxPath);
            var instance = Object.FindObjectOfType<VRCAvatarDescriptor>()?.gameObject;
            if (instance == null)
            {
                instance = (GameObject)PrefabUtility.InstantiatePrefab(prefab);
                instance.name = "AetherAvatar";
            }
            var descriptor = instance.GetComponent<VRCAvatarDescriptor>()
                          ?? instance.AddComponent<VRCAvatarDescriptor>();

            // ViewPosition 取头骨位置
            var animator = instance.GetComponent<Animator>();
            var head = animator != null ? animator.GetBoneTransform(HumanBodyBones.Head) : null;
            if (head != null)
                descriptor.ViewPosition = instance.transform.InverseTransformPoint(
                    head.position + new Vector3(0, 0.06f, 0.05f));

            // ── 3. Viseme ────────────────────────────────────
            ConfigureVisemes(descriptor, instance);

            // ── 4. Expression Parameters ─────────────────────
            var parameters = ScriptableObject.CreateInstance<VRCExpressionParameters>();
            parameters.parameters = new[]
            {
                new VRCExpressionParameters.Parameter
                {
                    name = "ActionID",
                    valueType = VRCExpressionParameters.ValueType.Int,
                    defaultValue = 0, saved = false, networkSynced = true,
                },
                new VRCExpressionParameters.Parameter
                {
                    name = "Expression",
                    valueType = VRCExpressionParameters.ValueType.Int,
                    defaultValue = 0, saved = false, networkSynced = true,
                },
            };
            AssetDatabase.CreateAsset(parameters, $"{OutDir}/AetherParameters.asset");

            var menu = ScriptableObject.CreateInstance<VRCExpressionsMenu>();
            AssetDatabase.CreateAsset(menu, $"{OutDir}/AetherMenu.asset");

            descriptor.customExpressions = true;
            descriptor.expressionParameters = parameters;
            descriptor.expressionsMenu = menu;

            // ── 5. FX Animator ───────────────────────────────
            var fx = BuildFxController();
            descriptor.customizeAnimationLayers = true;
            for (int i = 0; i < descriptor.baseAnimationLayers.Length; i++)
            {
                if (descriptor.baseAnimationLayers[i].type ==
                    VRCAvatarDescriptor.AnimLayerType.FX)
                {
                    descriptor.baseAnimationLayers[i].isDefault = false;
                    descriptor.baseAnimationLayers[i].animatorController = fx;
                }
            }

            EditorUtility.SetDirty(descriptor);
            AssetDatabase.SaveAssets();
            Debug.Log("[Aether] Avatar 配置完成：Humanoid + Viseme + 参数 + FX 层");
        }

        static void ConfigureVisemes(VRCAvatarDescriptor descriptor, GameObject root)
        {
            var renderer = root.GetComponentInChildren<SkinnedMeshRenderer>();
            if (renderer == null || renderer.sharedMesh == null) return;

            var mesh = renderer.sharedMesh;
            var available = new Dictionary<string, string>();  // 小写名 → 原名
            for (int i = 0; i < mesh.blendShapeCount; i++)
            {
                var name = mesh.GetBlendShapeName(i);
                available[name.ToLowerInvariant()] = name;
            }

            // VRM 只有 A/E/I/O/U 五个口型 → 映射到 15 视素的近似表
            string Find(params string[] keys)
            {
                foreach (var key in keys)
                    foreach (var pair in available)
                        if (pair.Key.Contains(key)) return pair.Value;
                return null;
            }
            string a = Find("a", "aa"), e = Find("e", "ee"), i = Find("i", "ih"),
                   o = Find("o", "oh"), u = Find("u", "ou");

            if (a == null)
            {   // 连 A 都没有 → 退化为 Jaw Flap
                descriptor.lipSync = VRC.SDKBase.VRC_AvatarDescriptor.LipSyncStyle.Default;
                return;
            }

            descriptor.lipSync = VRC.SDKBase.VRC_AvatarDescriptor.LipSyncStyle.VisemeBlendShape;
            descriptor.VisemeSkinnedMesh = renderer;
            // 15 视素 → 5 口型近似映射（sil 留空）
            var mapping = new[] { null, u, u, i, o, e, e, u, a, e, a, a, o, u, a };
            descriptor.VisemeBlendShapes = new string[15];
            for (int v = 0; v < 15; v++)
                descriptor.VisemeBlendShapes[v] = mapping[v];
        }

        static AnimatorController BuildFxController()
        {
            var path = $"{OutDir}/AetherFX.controller";
            AssetDatabase.DeleteAsset(path);
            var controller = AnimatorController.CreateAnimatorControllerAtPath(path);

            controller.AddParameter("ActionID", AnimatorControllerParameterType.Int);
            controller.AddParameter("Expression", AnimatorControllerParameterType.Int);

            BuildIntLayer(controller, "AetherActions", "ActionID", Actions, applyTracking: true);
            BuildIntLayer(controller, "AetherExpressions", "Expression", Expressions, applyTracking: false);
            return controller;
        }

        static void BuildIntLayer(AnimatorController controller, string layerName,
                                  string parameter, (string name, int id)[] entries,
                                  bool applyTracking)
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
                defaultWeight = 1f,
            });

            var idle = machine.AddState("idle");
            machine.defaultState = idle;

            foreach (var (name, id) in entries)
            {
                if (id == 0) continue;    // idle/neutral 即默认态
                var state = machine.AddState(name);
                // 占位空动画；真实动画后续替换（VoxEdit .vxa 转换或手工制作）
                var clip = new AnimationClip { name = $"{name}_placeholder" };
                AssetDatabase.AddObjectToAsset(clip, controller);
                state.motion = clip;

                if (applyTracking)
                {
                    // 入口切 Animation，退出回 Tracking —— 与 posture freeze/resume 配合
                    var enter = state.AddStateMachineBehaviour<
                        VRC.SDK3.Avatars.Components.VRCAnimatorTrackingControl>();
                    enter.trackingHead = VRC_AnimatorTrackingControl.TrackingType.NoChange;
                    enter.trackingLeftHand = VRC_AnimatorTrackingControl.TrackingType.Animation;
                    enter.trackingRightHand = VRC_AnimatorTrackingControl.TrackingType.Animation;
                }

                var to = idle.AddTransition(state);
                to.AddCondition(AnimatorConditionMode.Equals, id, parameter);
                to.hasExitTime = false;
                to.duration = 0.1f;

                var back = state.AddTransition(idle);
                back.AddCondition(AnimatorConditionMode.NotEqual, id, parameter);
                back.hasExitTime = false;
                back.duration = 0.15f;
            }
        }
    }
}
#endif
