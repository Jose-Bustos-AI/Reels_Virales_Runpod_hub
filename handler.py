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
