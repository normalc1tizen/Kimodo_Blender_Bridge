"""
Kimodo Blender Bridge — Gradio Client
Handles all HTTP communication with the Kimodo Gradio demo (localhost:7860).

Gradio 4.x API shape we target:
  GET  /info                 → discover named endpoints + their parameter schemas
  POST /run/{fn_name}        → synchronous predict (may time-out for long jobs)
  POST /queue/join           → submit job, returns {event_id}
  GET  /queue/status         → SSE stream; we poll with regular GET for simplicity
  GET  /file={server_path}   → download a generated file
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import tempfile
import os
import time


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, payload: dict, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def test_connection(base_url: str) -> tuple[bool, str]:
    """
    Returns (success: bool, message: str).
    Hits /info to verify a Gradio app is running at base_url.
    """
    base_url = base_url.rstrip("/")
    try:
        info = _get(f"{base_url}/info", timeout=8)
        endpoints = list(info.get("named_endpoints", {}).keys())
        n = len(endpoints)
        return True, f"Connected ✓  ({n} endpoint{'s' if n != 1 else ''} found)"
    except urllib.error.URLError as e:
        return False, f"Connection refused — is Kimodo running? ({e.reason})"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def get_endpoints(base_url: str) -> list[tuple[str, dict]]:
    """
    Returns a list of (endpoint_name, schema_dict) tuples from /info.
    Empty list on failure.
    """
    base_url = base_url.rstrip("/")
    try:
        info = _get(f"{base_url}/info", timeout=8)
        named = info.get("named_endpoints", {})
        return list(named.items())
    except Exception:
        return []


def generate_motion(
    base_url: str,
    endpoint: str,
    prompt: str,
    duration: float,
    model: str,          # "smpl" or "smplx"
    seed: int,
    output_format: str,  # "bvh" or "npz"
    constraints_json: str | None = None,   # JSON string or None
    progress_callback=None,  # callable(str) for status updates
) -> tuple[bool, str]:
    """
    Submits a generation job to Kimodo via the Gradio queue API.
    Polls until completion.

    Returns (success: bool, result: str)
      On success  → result is the local file path of the downloaded BVH/NPZ
      On failure  → result is an error message
    """
    base_url = base_url.rstrip("/")
    endpoint = endpoint.lstrip("/")

    # --- Build the data payload -------------------------------------------
    # Kimodo's Gradio demo likely accepts positional args matching the UI.
    # The order is inferred from the demo documentation:
    #   [prompt, duration_seconds, model_type, seed, output_format, constraints_json]
    # constraints_json is either None or a JSON string.
    payload = {
        "data": [prompt, duration, model, seed, output_format, constraints_json],
    }

    if progress_callback:
        progress_callback("Submitting job to Kimodo…")

    # --- Try direct /run/{endpoint} first (works for quick jobs) ----------
    try:
        result = _post(f"{base_url}/run/{endpoint}", payload, timeout=300)
        data = result.get("data", [])
        if data:
            return _extract_and_download_file(base_url, data, output_format, progress_callback)
        return False, "Kimodo returned empty data."

    except urllib.error.HTTPError as e:
        if e.code == 404:
            # endpoint name doesn't exist under /run — try queue
            pass
        else:
            return False, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return False, f"Network error: {e.reason}"

    # --- Fall back to queue API -------------------------------------------
    if progress_callback:
        progress_callback("Joining generation queue…")

    try:
        join_payload = {"fn_index": 0, "data": payload["data"], "session_hash": "blender"}
        join_resp = _post(f"{base_url}/queue/join", join_payload, timeout=30)
        event_id = join_resp.get("event_id")
        if not event_id:
            return False, "Queue join failed — no event_id returned."
    except Exception as e:
        return False, f"Queue join failed: {e}"

    # Poll /queue/status (non-streaming GET with event_id param)
    poll_url = f"{base_url}/queue/status?event_id={urllib.parse.quote(event_id)}"
    deadline = time.time() + 600  # 10 minute timeout

    while time.time() < deadline:
        try:
            status = _get(poll_url, timeout=15)
            msg = status.get("msg", "")

            if progress_callback:
                progress_callback(f"Kimodo: {msg}")

            if msg == "process_completed":
                output = status.get("output", {})
                data = output.get("data", [])
                if data:
                    return _extract_and_download_file(base_url, data, output_format, progress_callback)
                return False, "Generation completed but no output data."

            if msg in ("queue_full", "estimation", "send_hash", "send_data",
                       "process_starts", "process_generating", "heartbeat"):
                time.sleep(2)
                continue

            if "error" in msg.lower() or status.get("success") is False:
                return False, f"Kimodo error: {status}"

        except Exception as e:
            # Transient poll failure — retry
            time.sleep(3)
            continue

    return False, "Generation timed out after 10 minutes."


def _extract_and_download_file(
    base_url: str,
    data: list,
    output_format: str,
    progress_callback=None,
) -> tuple[bool, str]:
    """
    Extracts a file URL or path from Gradio response data and downloads it.
    Returns (success, local_path_or_error).
    """
    # Gradio file outputs can be:
    #   {"path": "/tmp/...", "url": "http://host/file=...", "orig_name": "motion.bvh"}
    # or just a string path/URL, or nested in a list.

    file_info = None
    for item in data:
        if isinstance(item, dict) and ("url" in item or "path" in item):
            file_info = item
            break
        if isinstance(item, str) and (item.endswith(".bvh") or item.endswith(".npz")):
            file_info = {"url": item}
            break

    if not file_info:
        # Last resort: look for any string that looks like a file URL
        for item in data:
            if isinstance(item, str) and ("file=" in item or "/tmp/" in item):
                file_info = {"url": item}
                break

    if not file_info:
        return False, f"Could not locate file in response: {data}"

    # Resolve the download URL
    download_url = file_info.get("url") or file_info.get("path", "")
    if not download_url.startswith("http"):
        # It's a server-side path — build the Gradio file URL
        download_url = f"{base_url}/file={download_url}"

    if progress_callback:
        progress_callback("Downloading motion file…")

    # Download to a temp file
    ext = ".bvh" if output_format == "bvh" else ".npz"
    orig_name = file_info.get("orig_name", f"kimodo_motion{ext}")
    suffix = os.path.splitext(orig_name)[1] or ext

    try:
        tmp = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=suffix,
            prefix="kimodo_",
        )
        req = urllib.request.Request(download_url)
        with urllib.request.urlopen(req, timeout=60) as resp:
            tmp.write(resp.read())
        tmp.close()

        if progress_callback:
            progress_callback(f"Downloaded → {os.path.basename(tmp.name)}")

        return True, tmp.name

    except Exception as e:
        return False, f"Download failed: {e}"


def download_file_to_path(url: str, dest_path: str) -> bool:
    """Utility: download any URL to a specific path. Returns True on success."""
    try:
        urllib.request.urlretrieve(url, dest_path)
        return True
    except Exception:
        return False
