import runpod
import os
import websocket
import json
import uuid
import logging
import urllib.request
import urllib.parse
import subprocess
import time
import base64
import binascii
import requests
import glob
from pathlib import Path
from typing import Optional, Dict, Any, List

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

server_address = os.getenv("SERVER_ADDRESS", "127.0.0.1")
client_id = str(uuid.uuid4())

OUTPUT_DIRS = ["/ComfyUI/output", "/ComfyUI/user/output", "/tmp"]

# -------------------------
# IO helpers
# -------------------------
def download_file_from_url(url: str, output_path: str) -> str:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    result = subprocess.run(
        ["wget", "-L", "-O", output_path, "--no-verbose", "--timeout=30", "--tries=3", "--retry-connrefused", url],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        logger.info(f"✅ Download OK: {url} -> {output_path} ({os.path.getsize(output_path)} bytes)")
        return output_path
    raise Exception(f"URL download failed: {result.stderr}")


def save_base64_to_file(base64_data: str, temp_dir: str, output_filename: str) -> str:
    try:
        if not isinstance(base64_data, str):
            raise Exception("base64_data is not a string")

        b64 = base64_data.strip()
        # supports data-uri
        if "base64," in b64:
            b64 = b64.split("base64,", 1)[1].strip()

        missing = (-len(b64)) % 4
        if missing:
            b64 += "=" * missing

        decoded = base64.b64decode(b64, validate=False)

        os.makedirs(temp_dir, exist_ok=True)
        file_path = os.path.abspath(os.path.join(temp_dir, output_filename))
        with open(file_path, "wb") as f:
            f.write(decoded)

        if os.path.getsize(file_path) <= 0:
            raise Exception("decoded base64 produced empty file")

        logger.info(f"✅ Base64 saved: {file_path}")
        return file_path
    except (binascii.Error, ValueError) as e:
        raise Exception(f"Base64 decode failed: {e}")


def process_input(input_data, temp_dir: str, output_filename: str, input_type: str) -> str:
    if input_type == "path":
        return input_data
    if input_type == "url":
        return download_file_from_url(input_data, os.path.abspath(os.path.join(temp_dir, output_filename)))
    if input_type == "base64":
        return save_base64_to_file(input_data, temp_dir, output_filename)
    raise Exception(f"Unsupported input type: {input_type}")


# -------------------------
# Comfy helpers
# -------------------------
def load_workflow(workflow_path: str) -> Dict[str, Any]:
    with open(workflow_path, "r", encoding="utf-8") as f:
        return json.load(f)


def queue_prompt(prompt: Dict[str, Any]) -> Dict[str, Any]:
    url = f"http://{server_address}:8188/prompt"
    payload = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    req.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(req).read())


def get_history(prompt_id: str) -> Dict[str, Any]:
    url = f"http://{server_address}:8188/history/{prompt_id}"
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())


def view_download(filename: str, subfolder: str, folder_type: str) -> bytes:
    url = f"http://{server_address}:8188/view"
    data = {"filename": filename, "subfolder": subfolder or "", "type": folder_type or "output"}
    url_values = urllib.parse.urlencode(data)
    with urllib.request.urlopen(f"{url}?{url_values}") as response:
        return response.read()


def wait_for_comfyui():
    http_url = f"http://{server_address}:8188/"
    for i in range(600):
        try:
            urllib.request.urlopen(http_url, timeout=5)
            logger.info(f"✅ ComfyUI ready (attempt {i+1})")
            return
        except Exception:
            time.sleep(1)
    raise Exception("ComfyUI not reachable via HTTP")


def find_newest_mp4(prefix: Optional[str] = None) -> Optional[str]:
    candidates: List[str] = []
    for d in OUTPUT_DIRS:
        candidates += glob.glob(f"{d}/**/*.mp4", recursive=True)
    candidates = [p for p in candidates if os.path.exists(p) and os.path.getsize(p) > 0]
    if prefix:
        candidates = [p for p in candidates if Path(p).name.startswith(prefix)]
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def run_and_get_mp4(prompt: Dict[str, Any], filename_prefix: str) -> str:
    wait_for_comfyui()

    ws_url = f"ws://{server_address}:8188/ws?clientId={client_id}"
    ws = websocket.WebSocket()
    ws.connect(ws_url)

    prompt_id = queue_prompt(prompt)["prompt_id"]
    logger.info(f"▶️ Running workflow prompt_id={prompt_id}")

    # wait until finished
    while True:
        out = ws.recv()
        if isinstance(out, str):
            msg = json.loads(out)
            if msg.get("type") == "executing":
                data = msg.get("data", {})
                if data.get("node") is None and data.get("prompt_id") == prompt_id:
                    break

    ws.close()

    history = get_history(prompt_id).get(prompt_id, {})
    outputs = history.get("outputs", {})

    # Read outputs (videos/gifs/images and ui nested)
    for _, node_output in outputs.items():
        ui = node_output.get("ui")
        if isinstance(ui, dict):
            for k in ("videos", "gifs", "images"):
                items = ui.get(k)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            fp = item.get("fullpath")
                            if fp and os.path.exists(fp) and os.path.getsize(fp) > 0:
                                return fp
                            fn = item.get("filename")
                            if fn:
                                data = view_download(fn, item.get("subfolder", ""), item.get("type", "output"))
                                tmp = f"/tmp/{uuid.uuid4().hex}_{fn}"
                                with open(tmp, "wb") as f:
                                    f.write(data)
                                if os.path.getsize(tmp) > 0:
                                    if not tmp.lower().endswith(".mp4"):
                                        tmp2 = tmp + ".mp4"
                                        os.rename(tmp, tmp2)
                                        tmp = tmp2
                                    return tmp

        for k in ("videos", "gifs", "images"):
            items = node_output.get(k)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        fp = item.get("fullpath")
                        if fp and os.path.exists(fp) and os.path.getsize(fp) > 0:
                            return fp
                        fn = item.get("filename")
                        if fn:
                            data = view_download(fn, item.get("subfolder", ""), item.get("type", "output"))
                            tmp = f"/tmp/{uuid.uuid4().hex}_{fn}"
                            with open(tmp, "wb") as f:
                                f.write(data)
                            if os.path.getsize(tmp) > 0:
                                if not tmp.lower().endswith(".mp4"):
                                    tmp2 = tmp + ".mp4"
                                    os.rename(tmp, tmp2)
                                    tmp = tmp2
                                return tmp

    # Filesystem fallback
    mp4 = find_newest_mp4(prefix=filename_prefix)
    if mp4:
        return mp4

    raise Exception("Could not find MP4 output (history empty and no mp4 on disk).")


# -------------------------
# Supabase upload
# -------------------------
def supabase_upload_file(local_path: str, dest_path: str) -> str:
    supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    bucket = os.environ.get("SUPABASE_BUCKET", "results")

    upload_url = f"{supabase_url}/storage/v1/object/{bucket}/{dest_path}"

    with open(local_path, "rb") as f:
        r = requests.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {key}",
                "apikey": key,
                "Content-Type": "video/mp4",
                "x-upsert": "true",
            },
            data=f,
            timeout=300,
        )

    if not r.ok:
        raise Exception(f"Supabase upload failed: {r.status_code} {r.text}")

    return f"{supabase_url}/storage/v1/object/public/{bucket}/{dest_path}"


# -------------------------
# Param injection helpers
# -------------------------
def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def force_sdpa(prompt: Dict[str, Any]) -> None:
    # deterministic ID if present
    if "22" in prompt and isinstance(prompt["22"], dict):
        inputs = prompt["22"].setdefault("inputs", {})
        if "attention_mode" in inputs and inputs.get("attention_mode") != "sdpa":
            old = inputs.get("attention_mode")
            inputs["attention_mode"] = "sdpa"
            logger.info(f"✅ attention_mode forced sdpa on node 22 (was {old})")

    # fallback by class_type
    for nid, node in prompt.items():
        if isinstance(node, dict) and node.get("class_type") == "WanVideoModelLoader":
            inputs = node.setdefault("inputs", {})
            if inputs.get("attention_mode") != "sdpa":
                old = inputs.get("attention_mode", "auto")
                inputs["attention_mode"] = "sdpa"
                logger.info(f"✅ attention_mode forced sdpa on WanVideoModelLoader node {nid} (was {old})")


def apply_denoise_strength(prompt: Dict[str, Any], job_input: Dict[str, Any]) -> None:
    if "denoise_strength" not in job_input or job_input["denoise_strength"] is None:
        return

    try:
        val = float(job_input["denoise_strength"])
    except Exception:
        raise Exception("denoise_strength must be numeric")

    # rango sano (evita corrupción por valores extremos)
    val = clamp(val, 0.05, 1.0)

    applied = False

    # If explicit nodes exist (I2V_WAN22 case)
    for nid in ("139", "140"):
        if nid in prompt and isinstance(prompt[nid], dict):
            prompt[nid].setdefault("inputs", {})["denoise_strength"] = val
            logger.info(f"✅ denoise_strength applied to node {nid} -> {val}")
            applied = True

    # If WanAnimate uses node 27
    if not applied and "27" in prompt and isinstance(prompt["27"], dict):
        prompt["27"].setdefault("inputs", {})["denoise_strength"] = val
        logger.info(f"✅ denoise_strength applied to node 27 -> {val}")
        applied = True

    # Generic: apply to every WanVideoSampler node
    for nid, node in prompt.items():
        if isinstance(node, dict) and node.get("class_type") == "WanVideoSampler":
            node.setdefault("inputs", {})["denoise_strength"] = val
            logger.info(f"✅ denoise_strength applied to WanVideoSampler node {nid} -> {val}")
            applied = True

    if not applied:
        logger.warning("⚠️ denoise_strength provided but no WanVideoSampler nodes found; ignored")


def apply_face_pose_strength(prompt: Dict[str, Any], job_input: Dict[str, Any]) -> None:
    # Node 198 in your workflow: AdaptiveWanVideoAnimateEmbeds
    if "198" not in prompt or not isinstance(prompt["198"], dict):
        # do not crash; just warn
        if ("face_strength" in job_input and job_input["face_strength"] is not None) or (
            "pose_strength" in job_input and job_input["pose_strength"] is not None
        ):
            logger.warning("⚠️ Node 198 not found; face_strength/pose_strength ignored")
        return

    inputs = prompt["198"].setdefault("inputs", {})

    if "face_strength" in job_input and job_input["face_strength"] is not None:
        try:
            val = float(job_input["face_strength"])
        except Exception:
            raise Exception("face_strength must be numeric")
        val = clamp(val, 0.5, 1.5)
        inputs["face_strength"] = val
        logger.info(f"✅ face_strength applied to node 198 -> {val}")

    if "pose_strength" in job_input and job_input["pose_strength"] is not None:
        try:
            val = float(job_input["pose_strength"])
        except Exception:
            raise Exception("pose_strength must be numeric")
        val = clamp(val, 0.5, 1.5)
        inputs["pose_strength"] = val
        logger.info(f"✅ pose_strength applied to node 198 -> {val}")


# -------------------------
# Handler
# -------------------------
def handler(job):
    job_input = job.get("input", {}) or {}

    # Required params (keep aligned with your current infra)
    for k in ("prompt", "seed", "width", "height", "fps", "cfg"):
        if k not in job_input:
            return {"error": f"Missing required field: {k}"}

    prompt_text = job_input["prompt"]
    seed = int(job_input["seed"])
    width = int(job_input["width"])
    height = int(job_input["height"])
    fps = int(job_input["fps"])
    cfg = float(job_input["cfg"])
    steps = int(job_input.get("steps", 4))

    # Duration cap (seconds): default 8, max 10
    duration_sec = int(job_input.get("duration_sec", job_input.get("max_video_seconds", 8)))
    duration_sec = min(max(duration_sec, 1), 10)
    frame_cap = fps * duration_sec

    task_id = f"wanimate_{uuid.uuid4().hex}"
    temp_dir = f"/tmp/{task_id}"
    os.makedirs(temp_dir, exist_ok=True)

    # IMAGE input
    if "image_path" in job_input:
        image_path = process_input(job_input["image_path"], temp_dir, "input_image.png", "path")
    elif "image_url" in job_input:
        image_path = process_input(job_input["image_url"], temp_dir, "input_image.png", "url")
    elif "image_base64" in job_input:
        image_path = process_input(job_input["image_base64"], temp_dir, "input_image.png", "base64")
    else:
        return {"error": "Image input required (image_path|image_url|image_base64)"}

    # VIDEO input
    if "video_path" in job_input:
        video_path = process_input(job_input["video_path"], temp_dir, "input_video.mp4", "path")
    elif "video_url" in job_input:
        video_path = process_input(job_input["video_url"], temp_dir, "input_video.mp4", "url")
    elif "video_base64" in job_input:
        video_path = process_input(job_input["video_base64"], temp_dir, "input_video.mp4", "base64")
    else:
        return {"error": "Video input required (video_path|video_url|video_base64)"}

    has_points = job_input.get("points_store") is not None
    mode = job_input.get("mode", "replace")  # replace|animate

    # IMPORTANT: keep the SAME workflows you already used when it worked
    if has_points:
        workflow_path = "/newWanAnimate_point_animate_api.json" if mode == "animate" else "/newWanAnimate_point_api.json"
    else:
        workflow_path = "/newWanAnimate_noSAM_animate_api.json" if mode == "animate" else "/newWanAnimate_noSAM_api.json"

    # Load workflow
    try:
        prompt = load_workflow(workflow_path)
    except FileNotFoundError:
        return {"error": f"Workflow not found in container: {workflow_path}"}
    except Exception as e:
        return {"error": f"Failed to load workflow {workflow_path}: {str(e)}"}

    # -------------------------
    # Stability overrides (DO NOT TOUCH OTHER LOGIC)
    # -------------------------
    # SDPA fix: avoid sageattn SM90 issues
    force_sdpa(prompt)

    # Node 30: ensure output saved + unique prefix (if node exists)
    if "30" in prompt and isinstance(prompt["30"], dict):
        p30 = prompt["30"].setdefault("inputs", {})
        p30["save_output"] = True
        p30["filename_prefix"] = task_id
        p30["frame_rate"] = fps

    # Inject main inputs (IDs from your workflow)
    # Node 57: LoadImage
    if "57" in prompt and isinstance(prompt["57"], dict):
        prompt["57"].setdefault("inputs", {})["image"] = image_path

    # Node 63: VHS_LoadVideo
    if "63" in prompt and isinstance(prompt["63"], dict):
        p63 = prompt["63"].setdefault("inputs", {})
        p63["video"] = video_path
        p63["force_rate"] = fps
        p63["frame_load_cap"] = frame_cap  # ✅ duration limiter

    # Node 65: WanVideoTextEncodeCached
    if "65" in prompt and isinstance(prompt["65"], dict):
        p65 = prompt["65"].setdefault("inputs", {})
        p65["positive_prompt"] = prompt_text
        if "negative_prompt" in job_input and job_input["negative_prompt"] is not None:
            p65["negative_prompt"] = job_input["negative_prompt"]

    # Node 27: WanVideoSampler (seed/cfg/steps)
    if "27" in prompt and isinstance(prompt["27"], dict):
        p27 = prompt["27"].setdefault("inputs", {})
        p27["seed"] = seed
        p27["cfg"] = cfg
        p27["steps"] = steps

    # Resolution nodes 150/151 (INTConstant)
    if "150" in prompt and isinstance(prompt["150"], dict):
        prompt["150"].setdefault("inputs", {})["value"] = width
    if "151" in prompt and isinstance(prompt["151"], dict):
        prompt["151"].setdefault("inputs", {})["value"] = height

    # Points mode nodes
    if has_points:
        if "107" in prompt and isinstance(prompt["107"], dict):
            p107 = prompt["107"].setdefault("inputs", {})
            p107["points_store"] = job_input.get("points_store")
            p107["coordinates"] = job_input.get("coordinates")
            p107["neg_coordinates"] = job_input.get("neg_coordinates")

    # ✅ NEW: apply denoise_strength globally to sampler nodes (safe)
    apply_denoise_strength(prompt, job_input)

    # ✅ NEW: face_strength + pose_strength to node 198 (safe)
    apply_face_pose_strength(prompt, job_input)

    # Execute and retrieve mp4
    try:
        mp4_path = run_and_get_mp4(prompt, filename_prefix=task_id)
    except Exception as e:
        logger.exception("ComfyUI execution failed")
        return {"error": f"ComfyUI execution failed: {str(e)}"}

    # Upload & return URL
    for k in ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"]:
        if not os.environ.get(k):
            return {"error": f"Missing env var: {k}"}

    prefix = os.environ.get("SUPABASE_PATH_PREFIX", "wananimate").strip("/")
    dest_path = f"{prefix}/{task_id}.mp4" if prefix else f"{task_id}.mp4"

    try:
        video_url = supabase_upload_file(mp4_path, dest_path)
    except Exception as e:
        logger.exception("Supabase upload failed")
        return {"error": f"Supabase upload failed: {str(e)}"}

    return {
        "video_url": video_url,
        "duration_sec": duration_sec,
        "fps": fps,
        "frames": frame_cap,
        "seed": seed,
        "cfg": cfg,
        "steps": steps,
        "denoise_strength": job_input.get("denoise_strength"),
        "face_strength": job_input.get("face_strength"),
        "pose_strength": job_input.get("pose_strength"),
        "workflow_path": workflow_path,
    }


runpod.serverless.start({"handler": handler})
