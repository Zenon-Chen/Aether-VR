"""DART Motion Generation HTTP Server

GPU-agnostic: works on both NVIDIA (CUDA) and AMD (ROCm).

启动:
    cd dart && uvicorn server:app --host 0.0.0.0 --port 8900

环境变量:
    DART_CHECKPOINT  —  模型权重路径 (默认: mld_denoiser/mld_fps_clip_repeat_euler/checkpoint_300000.pt)
"""

from __future__ import annotations

import os
import sys
import time
import random
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import torch

# Ensure working directory is DART root so relative imports work
DART_ROOT = Path(__file__).resolve().parent
os.chdir(DART_ROOT)
sys.path.insert(0, str(DART_ROOT))

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from model.mld_denoiser import DenoiserMLP, DenoiserTransformer
from model.mld_vae import AutoMldVae
from data_loaders.humanml.data.dataset import SinglePrimitiveDataset
from utils.smpl_utils import tensor_dict_to_device
from utils.misc_util import encode_text, compose_texts_with_and
from utils import rotation_conversions as transforms
from mld.rollout_mld import load_mld, RolloutArgs, ClassifierFreeWrapper
from mld.train_mld import create_gaussian_diffusion


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CHECKPOINT_PATH = os.environ.get(
    'DART_CHECKPOINT',
    str(DART_ROOT / 'mld_denoiser' / 'mld_fps_clip_repeat_euler' / 'checkpoint_300000.pt'),
)
DEVICE_STR = 'cuda'
DEFAULT_BATCH_SIZE = 1
DEFAULT_GUIDANCE = 5.0
DEFAULT_RESPACING = ''
MAX_SEGMENTS = 30
DATASET_TYPE = 'babel'


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
class ModelStore:
    """Holds all loaded model objects."""
    def __init__(self):
        self.denoiser_args = None
        self.denoiser_model = None
        self.vae_args = None
        self.vae_model = None
        self.diffusion = None
        self.dataset = None
        self.device = None
        self.loaded = False
        self.generating = False
        self.lock: Optional[asyncio.Lock] = None

store = ModelStore()


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load model once on startup."""
    print('[Server] Loading DART model...')
    t0 = time.time()

    device = torch.device(DEVICE_STR if torch.cuda.is_available() else 'cpu')
    store.device = device

    # Seed
    random.seed(0)
    np.random.seed(0)
    torch.manual_seed(0)
    torch.set_default_dtype(torch.float32)
    torch.backends.cudnn.deterministic = True

    # Load denoiser + VAE
    denoiser_args, denoiser_model, vae_args, vae_model = load_mld(CHECKPOINT_PATH, device)
    store.denoiser_args = denoiser_args
    store.denoiser_model = denoiser_model
    store.vae_args = vae_args
    store.vae_model = vae_model

    # Diffusion
    diffusion_args = denoiser_args.diffusion_args
    diffusion_args.respacing = DEFAULT_RESPACING
    store.diffusion = create_gaussian_diffusion(diffusion_args)

    # Dataset (SinglePrimitiveDataset — provides standing pose seed)
    sequence_path = './data/stand.pkl' if DATASET_TYPE == 'babel' else './data/stand_20fps.pkl'
    store.dataset = SinglePrimitiveDataset(
        cfg_path=vae_args.data_args.cfg_path,
        dataset_path=vae_args.data_args.data_dir,
        body_type=vae_args.data_args.body_type,
        sequence_path=sequence_path,
        batch_size=DEFAULT_BATCH_SIZE,
        device=device,
        enforce_gender='male',
        enforce_zero_beta=1,
    )

    store.lock = asyncio.Lock()
    store.loaded = True
    elapsed = time.time() - t0
    print(f'[Server] Model loaded in {elapsed:.1f}s  device={device}')
    yield
    print('[Server] Shutting down.')


app = FastAPI(title='DART Motion Generation', lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    text: str = Field(..., description='Text prompt, e.g. "walk forward*5"')
    batch_size: int = Field(default=1, ge=1, le=8)
    guidance_param: float = Field(default=5.0, ge=0.0, le=20.0)


class GenerateResponse(BaseModel):
    poses: list            # (T, 165)
    trans: list            # (T, 3)
    betas: list            # (10,)
    joints: list           # (T, 22, 3)
    framerate: int
    num_frames: int
    text_prompt: str


# ---------------------------------------------------------------------------
# Core generation logic (extracted from rollout_mld.rollout)
# ---------------------------------------------------------------------------
def generate_motion(
    text_prompt: str,
    batch_size: int = 1,
    guidance_param: float = 5.0,
) -> dict:
    """Run DART inference and return motion data as numpy arrays.

    Returns dict with keys: poses, trans, betas, joints, framerate, num_frames, text_prompt
    """
    denoiser_args = store.denoiser_args
    denoiser_model = store.denoiser_model
    vae_args = store.vae_args
    vae_model = store.vae_model
    diffusion = store.diffusion
    dataset = store.dataset
    device = store.device

    future_length = dataset.future_length
    history_length = dataset.history_length
    primitive_length = history_length + future_length

    sample_fn = diffusion.p_sample_loop

    # ---- Parse text prompt (same logic as rollout_mld.py) ----
    texts = []
    if ',' in text_prompt:
        for segment in text_prompt.split(','):
            action, num_mp = segment.split('*')
            action = compose_texts_with_and(action.split(' and '))
            texts = texts + [action] * int(num_mp)
    else:
        action, num_rollout = text_prompt.split('*')
        action = compose_texts_with_and(action.split(' and '))
        num_rollout = int(num_rollout)
        for _ in range(num_rollout):
            texts.append(action)

    all_text_embedding = encode_text(
        dataset.clip_model, texts, force_empty_zero=True
    ).to(dtype=torch.float32, device=device)

    primitive_utility = dataset.primitive_utility

    # ---- Initial batch ----
    batch = dataset.get_batch(batch_size=batch_size)
    input_motions, model_kwargs = batch[0]['motion_tensor_normalized'], {'y': batch[0]}
    del model_kwargs['y']['motion_tensor_normalized']
    gender = model_kwargs['y']['gender'][0]
    betas = model_kwargs['y']['betas'][:, :primitive_length, :].to(device)
    pelvis_delta = primitive_utility.calc_calibrate_offset({
        'betas': betas[:, 0, :],
        'gender': gender,
    })
    input_motions = input_motions.to(device)
    motion_tensor = input_motions.squeeze(2).permute(0, 2, 1)
    history_motion_gt = motion_tensor[:, :history_length, :]

    # ---- Rollout loop ----
    motion_sequences = None
    history_motion = history_motion_gt
    transf_rotmat = torch.eye(3, device=device, dtype=torch.float32).unsqueeze(0).repeat(batch_size, 1, 1)
    transf_transl = torch.zeros(3, device=device, dtype=torch.float32).reshape(1, 1, 3).repeat(batch_size, 1, 1)

    for segment_id in range(len(texts)):
        text_embedding = all_text_embedding[segment_id].expand(batch_size, -1)
        gp = torch.ones(batch_size, *denoiser_args.model_args.noise_shape, device=device) * guidance_param
        y = {
            'text_embedding': text_embedding,
            'history_motion_normalized': history_motion,
            'scale': gp,
        }

        x_start_pred = sample_fn(
            denoiser_model,
            (batch_size, *denoiser_args.model_args.noise_shape),
            clip_denoised=False,
            model_kwargs={'y': y},
            skip_timesteps=0,
            init_image=None,
            progress=False,
            dump_steps=None,
            noise=None,
            const_noise=False,
        )
        latent_pred = x_start_pred.permute(1, 0, 2)
        future_motion_pred = vae_model.decode(
            latent_pred, history_motion, nfuture=future_length,
            scale_latent=denoiser_args.rescale_latent,
        )

        future_frames = dataset.denormalize(future_motion_pred)
        all_frames = torch.cat([dataset.denormalize(history_motion), future_frames], dim=1)

        if segment_id == 0:
            future_frames = all_frames

        future_feature_dict = primitive_utility.tensor_to_dict(future_frames)
        future_feature_dict.update({
            'transf_rotmat': transf_rotmat,
            'transf_transl': transf_transl,
            'gender': gender,
            'betas': betas[:, :future_length, :] if segment_id > 0 else betas[:, :primitive_length, :],
            'pelvis_delta': pelvis_delta,
        })
        future_primitive_dict = primitive_utility.feature_dict_to_smpl_dict(future_feature_dict)
        future_primitive_dict = primitive_utility.transform_primitive_to_world(future_primitive_dict)

        if motion_sequences is None:
            motion_sequences = future_primitive_dict
        else:
            for key in ['transl', 'global_orient', 'body_pose', 'betas', 'joints']:
                motion_sequences[key] = torch.cat(
                    [motion_sequences[key], future_primitive_dict[key]], dim=1
                )

        # Update history
        new_history_frames = all_frames[:, -history_length:, :]
        history_feature_dict = primitive_utility.tensor_to_dict(new_history_frames)
        history_feature_dict.update({
            'transf_rotmat': transf_rotmat,
            'transf_transl': transf_transl,
            'gender': gender,
            'betas': betas[:, :history_length, :],
            'pelvis_delta': pelvis_delta,
        })
        canonicalized, blended = primitive_utility.get_blended_feature(
            history_feature_dict, use_predicted_joints=0,
        )
        transf_rotmat = canonicalized['transf_rotmat']
        transf_transl = canonicalized['transf_transl']
        history_motion = primitive_utility.dict_to_tensor(blended)
        history_motion = dataset.normalize(history_motion)

    # ---- Extract results for batch index 0 ----
    seq_transl = motion_sequences['transl'][0].detach().cpu()           # (T, 3)
    seq_global_orient = motion_sequences['global_orient'][0].detach().cpu()  # (T, 1, 3, 3)
    seq_body_pose = motion_sequences['body_pose'][0].detach().cpu()    # (T, 21, 3, 3)
    seq_betas = motion_sequences['betas'][0].detach().cpu()            # (T, 10)
    seq_joints = motion_sequences['joints'][0].detach().cpu()          # (T, 66) -> reshape

    # Convert rotation matrices to axis-angle poses (T, 165)
    poses_66 = transforms.matrix_to_axis_angle(
        torch.cat([seq_global_orient.reshape(-1, 1, 3, 3), seq_body_pose], dim=1)
    ).reshape(-1, 22 * 3)  # (T, 66)
    poses_165 = torch.cat([poses_66, torch.zeros(poses_66.shape[0], 99)], dim=1)  # zero-pad hands/face

    num_frames = poses_165.shape[0]
    joints_22x3 = seq_joints.reshape(num_frames, 22, 3)
    betas_10 = seq_betas[0, :10]

    framerate = min(dataset.target_fps, 30)

    return {
        'poses': poses_165.numpy().tolist(),
        'trans': seq_transl.numpy().tolist(),
        'betas': betas_10.numpy().tolist(),
        'joints': joints_22x3.numpy().tolist(),
        'framerate': framerate,
        'num_frames': num_frames,
        'text_prompt': text_prompt,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get('/health')
async def health():
    gpu_available = torch.cuda.is_available()
    info = {
        'status': 'ok' if store.loaded else 'loading',
        'model_loaded': store.loaded,
        'gpu_available': gpu_available,
    }
    if gpu_available:
        info['gpu_device'] = torch.cuda.get_device_name(0)
        info['gpu_memory_used_mb'] = round(torch.cuda.memory_allocated(0) / 1024 / 1024, 1)
        info['gpu_memory_reserved_mb'] = round(torch.cuda.memory_reserved(0) / 1024 / 1024, 1)
    return info


@app.get('/status')
async def status():
    return {
        'model_loaded': store.loaded,
        'generating': store.generating,
        'checkpoint': CHECKPOINT_PATH,
        'device': str(store.device),
    }


@app.post('/generate', response_model=GenerateResponse)
async def generate(req: GenerateRequest):
    if not store.loaded:
        raise HTTPException(status_code=503, detail='Model not loaded yet')

    try:
        parts = [part.rsplit('*', 1) for part in req.text.split(',')]
        counts = [int(count) for action, count in parts if action.strip()]
        if len(counts) != len(parts) or any(count < 1 for count in counts):
            raise ValueError
        segment_count = sum(counts)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail='Prompt must use non-empty action*positive_segments format')
    if segment_count > MAX_SEGMENTS:
        raise HTTPException(status_code=400, detail=f'Total segments must be between 1 and {MAX_SEGMENTS}')

    if store.lock is None:
        store.lock = asyncio.Lock()
    if store.lock.locked():
        raise HTTPException(status_code=429, detail='Another generation is in progress')

    await store.lock.acquire()
    store.generating = True
    try:
        inference_task = asyncio.create_task(asyncio.to_thread(
            generate_motion, req.text, req.batch_size, req.guidance_param
        ))
        try:
            result = await asyncio.shield(inference_task)
        except asyncio.CancelledError:
            # GPU work in a thread cannot be cancelled. Keep the service lock until it
            # really finishes, otherwise a disconnected client can cause overlap.
            await inference_task
            raise
        return GenerateResponse(**result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        store.generating = False
        if store.lock.locked():
            store.lock.release()
