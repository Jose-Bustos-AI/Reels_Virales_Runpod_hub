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
def load_workflow(workflow_path: str):
    with open(workflow_path, "r") as f:
        return json.load(f)

def queue_prompt(prompt):
    url = f"http://{server_address}:8188/prompt"
    payload = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    req.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(req).read())

def get_history(prompt_id: str):
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

def find_newest_mp4(prefix=None):
    candidates = []
    for d in OUTPUT_DIRS:
        candidates += glob.glob(f"{d}/**/*.mp4", recursive=True)
    candidates = [p for p in candidates if os.path.exists(p) and os.path.getsize(p) > 0]
    if prefix:
        candidates = [p for p in candidates if Path(p).name.startswith(prefix)]
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]

def run_and_get_mp4(prompt, filename_prefix: str) -> str:
    wait_for_comfyui()

    ws_url = f"ws://{server_address}:8188/ws?clientId={client_id}"
    ws = websocket.WebSocket()
    ws.connect(ws_url)

    prompt_id = queue_prompt(prompt)["prompt_id"]
    logger.info(f"▶️ Running workflow prompt_id={prompt_id}")

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
# Handler
# -------------------------
def handler(job):
    job_input = job.get("input", {}) or {}

    # Required params
    prompt_text = job_input["prompt"]
    seed = int(job_input["seed"])
    width = int(job_input["width"])
    height = int(job_input["height"])
    fps = int(job_input["fps"])
    cfg = float(job_input["cfg"])
    steps = int(job_input.get("steps", 4))

    # Duration cap (seconds): default 8, max 10
    duration_sec = int(job_input.get("duration_sec", 8))
    duration_sec = min(max(duration_sec, 1), 10)
    frame_cap = fps * duration_sec

    task_id = f"wanimate_{uuid.uuid4().hex}"
    temp_dir = f"/tmp/{task_id}"
    os.makedirs(temp_dir, exist_ok=True)

    # Image
    if "image_path" in job_input:
        image_path = process_input(job_input["image_path"], temp_dir, "input_image.jpg", "path")
    elif "image_url" in job_input:
        image_path = process_input(job_input["image_url"], temp_dir, "input_image.jpg", "url")
    elif "image_base64" in job_input:
        image_path = process_input(job_input["image_base64"], temp_dir, "input_image.jpg", "base64")
    else:
        raise Exception("Image input required (image_path|image_url|image_base64)")

    # Video
    if "video_path" in job_input:
        video_path = process_input(job_input["video_path"], temp_dir, "input_video.mp4", "path")
    elif "video_url" in job_input:
        video_path = process_input(job_input["video_url"], temp_dir, "input_video.mp4", "url")
    elif "video_base64" in job_input:
        video_path = process_input(job_input["video_base64"], temp_dir, "input_video.mp4", "base64")
    else:
        raise Exception("Video input required (video_path|video_url|video_base64)")

    has_points = job_input.get("points_store") is not None
    mode = job_input.get("mode", "replace")  # replace|animate

    if has_points:
        workflow_path = "/newWanAnimate_point_animate_api.json" if mode == "animate" else "/newWanAnimate_point_api.json"
    else:
        workflow_path = "/newWanAnimate_noSAM_animate_api.json" if mode == "animate" else "/newWanAnimate_noSAM_api.json"

    prompt = load_workflow(workflow_path)

    # Stability overrides
    # Node 22: avoid sageattn dependency
    if "22" in prompt and "inputs" in prompt["22"]:
        prompt["22"]["inputs"]["attention_mode"] = "sdpa"

    # Node 30: ensure output saved + unique prefix
    if "30" in prompt and "inputs" in prompt["30"]:
        prompt["30"]["inputs"]["save_output"] = True
        prompt["30"]["inputs"]["filename_prefix"] = task_id

    # Inject parameters
    prompt["57"]["inputs"]["image"] = image_path
    prompt["63"]["inputs"]["video"] = video_path
    prompt["63"]["inputs"]["force_rate"] = fps
    prompt["63"]["inputs"]["frame_load_cap"] = frame_cap  # duration limiter
    prompt["30"]["inputs"]["frame_rate"] = fps

    prompt["65"]["inputs"]["positive_prompt"] = prompt_text
    if "negative_prompt" in job_input:
        prompt["65"]["inputs"]["negative_prompt"] = job_input["negative_prompt"]

    # Sampler
    prompt["27"]["inputs"]["seed"] = seed
    prompt["27"]["inputs"]["cfg"] = cfg
    prompt["27"]["inputs"]["steps"] = steps

    # ✅ SAFE denoise_strength injection (NO indentation traps, correct field name)
    if "denoise_strength" in job_input and job_input["denoise_strength"] is not None:
        ds = float(job_input["denoise_strength"])
        applied = False

        # Prefer known node 27 (as in your workflow)
        if "27" in prompt and "inputs" in prompt["27"]:
            prompt["27"]["inputs"]["denoise_strength"] = ds
            applied = True
            logger.info(f"✅ denoise_strength applied to node 27 -> {ds}")

        # Fallback: any WanVideoSampler node
        if not applied:
            for nid, node in prompt.items():
                if isinstance(node, dict) and node.get("class_type") == "WanVideoSampler":
                    node.setdefault("inputs", {})["denoise_strength"] = ds
                    applied = True
                    logger.info(f"✅ denoise_strength applied to WanVideoSampler node {nid} -> {ds}")

        if not applied:
            logger.warning("⚠️ denoise_strength provided but no WanVideoSampler found to apply it.")

    prompt["150"]["inputs"]["value"] = width
    prompt["151"]["inputs"]["value"] = height

    if has_points:
        prompt["107"]["inputs"]["points_store"] = job_input["points_store"]
        prompt["107"]["inputs"]["coordinates"] = job_input["coordinates"]
        prompt["107"]["inputs"]["neg_coordinates"] = job_input["neg_coordinates"]

    # Run and get mp4 path
    mp4_path = run_and_get_mp4(prompt, filename_prefix=task_id)

    # Upload & return URL
    for k in ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"]:
        if not os.environ.get(k):
            raise Exception(f"Missing env var: {k}")

    prefix = os.environ.get("SUPABASE_PATH_PREFIX", "wananimate").strip("/")
    dest_path = f"{prefix}/{task_id}.mp4" if prefix else f"{task_id}.mp4"
    video_url = supabase_upload_file(mp4_path, dest_path)

    return {"video_url": video_url, "duration_sec": duration_sec, "fps": fps, "frames": frame_cap}

runpod.serverless.start({"handler": handler})
