"""VLM inference server for granite-docling on Modal.

Cold-start strategy:
- `@modal.web_server` queues requests during cold start (no 503s).
- CPU + GPU memory snapshots capture vLLM warmed up and asleep, so restore
  only has to page the model back into GPU memory.
- HuggingFace + vLLM compile-cache volumes persist weights and torch-inductor
  artifacts across deploys, so the first snapshot-creation container is fast.
"""

import asyncio
import json
import socket
import subprocess
import time

import aiohttp
import modal

# --- Config -----------------------------------------------------------------

MINUTES = 60
PORT = 8000
APP_NAME = "vlm-endpoint-docworker-v1"

MODEL_NAME = "ibm-granite/granite-docling-258M"
MODEL_REVISION = "55d41aa24c0be047c7e4ed89b51058ea586b0428"

GPU = "L40S"
N_GPU = 1
REGION = "us-east"

MIN_CONTAINERS = 0                  # set to 1 to always keep a warm replica
MAX_INPUTS = 32                     # concurrent requests per replica before scaling up
FAST_BOOT = False                   # disables graph building on startup
SCALEDOWN_WINDOW = 2                # time till container is scaled down 
TIMEOUT = 2 * MINUTES               # time till execution fails 
STARTUP_TIMEOUT = 3 * MINUTES       # time till startup fails

# vLLM specific
MAX_NUM_SEQS = 32
MAX_MODEL_LEN = 8192
MAX_NUM_BATCHED_TOKENS = 8192

GPU_MEMORY_UTILIZATION = 0.9


# --- Image + volumes --------------------------------------------------------

HF_CACHE_PATH = "/root/.cache/huggingface"
VLLM_CACHE_PATH = "/root/.cache/vllm"
HF_CACHE_VOL = modal.Volume.from_name("huggingface-cache", create_if_missing=True)
VLLM_CACHE_VOL = modal.Volume.from_name("vllm-cache", create_if_missing=True)

vllm_image = (
    modal.Image.from_registry("nvidia/cuda:12.9.0-devel-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .uv_pip_install(
        "vllm==0.19.0",
    )
    .env({
        "VLLM_SERVER_DEV_MODE": "1",            # allows us to toggle server in and out of sleep mode for snapshot compatibility
        "HF_XET_HIGH_PERFORMANCE": "1",         # faster model transfers
        "TORCHINDUCTOR_COMPILE_THREADS": "1",   # improve compatibility with snapshots  
    })
)

# --- Helpers ----------------------------------------------------------------

with vllm_image.imports():
    import requests


def sleep(level=1):
    requests.post(
        f"http://localhost:{PORT}/sleep?level={level}"
    ).raise_for_status()


def wake_up():
    requests.post(f"http://localhost:{PORT}/wake_up").raise_for_status()


# waits for vLLM to be ready
def wait_ready(proc: subprocess.Popen):
    while True:
        try:
            socket.create_connection(("localhost", PORT), timeout=1).close()
            return
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(f"vLLM exited with {proc.returncode}")


# small warmup to ensure vLLM is ready
def warmup():
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": "Who are you?"}],
        "max_tokens": 16,
    }

    for _ in range(3):
        requests.post(
            f"http://localhost:{PORT}/v1/chat/completions",
            json=payload,
            timeout=300,
        ).raise_for_status()

# --- App --------------------------------------------------------------------

app = modal.App(name=APP_NAME)


@app.cls(
    image=vllm_image,
    gpu=f"{GPU}:{N_GPU}",
    scaledown_window=SCALEDOWN_WINDOW,
    timeout=TIMEOUT,
    volumes={HF_CACHE_PATH: HF_CACHE_VOL, VLLM_CACHE_PATH: VLLM_CACHE_VOL},
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
)
@modal.concurrent(max_inputs=MAX_INPUTS)
class VllmServer:
    @modal.enter(snap=True)
    def start(self):
        cmd = [
            "vllm", "serve", MODEL_NAME,
            "--host", "0.0.0.0",
            "--port", str(PORT),
        ]
        cmd += ["--tensor-parallel-size", str(N_GPU)]
        cmd += [
            "--enable-sleep-mode",
            # make KV cache predictable / small
            "--max-num-seqs", str(MAX_NUM_SEQS),
            "--max-model-len", str(MAX_MODEL_LEN),
            "--max-num-batched-tokens", str(MAX_NUM_BATCHED_TOKENS),
        ]

        print(*cmd)
        self.vllm_proc = subprocess.Popen(cmd)
        wait_ready(self.vllm_proc)
        warmup()
        sleep()

    @modal.enter(snap=False)
    def wake_up(self):
        wake_up()
        wait_ready(self.vllm_proc)

    @modal.web_server(port=PORT, startup_timeout=STARTUP_TIMEOUT)
    def serve(self):
        pass

    @modal.exit()
    def stop(self):
        self.vllm_proc.terminate()

# --- Local test entrypoint --------------------------------------------------

TEST_IMAGE_URL = (
    "https://huggingface.co/ibm-granite/granite-docling-258M/resolve/main/assets/new_arxiv.png"
)


@app.local_entrypoint()
async def test(test_timeout=10 * MINUTES, prompt=None, twice=True):
    url = await VllmServer().serve.get_web_url.aio()
    text = prompt or "Convert this image to Docling."

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": TEST_IMAGE_URL}},
                {"type": "text", "text": text},
            ],
        },
    ]
    print(f"Sending messages to {url}:", *messages, sep="\n\t")
    await _probe(url, messages, timeout=test_timeout)

    if twice:
        messages[0]["content"] = "You are a concise assistant. Answer in one sentence."
        print(f"\nSending messages to {url}:", *messages, sep="\n\t")
        await _probe(url, messages, timeout=1 * MINUTES)


async def _probe(url, messages, timeout=5 * MINUTES):
    deadline = time.time() + timeout
    async with aiohttp.ClientSession(base_url=url) as session:
        while time.time() < deadline:
            try:
                await _stream(session, messages)
                return
            except asyncio.TimeoutError:
                await asyncio.sleep(1)
    raise TimeoutError(f"No response from server within {timeout}s")


async def _stream(session: aiohttp.ClientSession, messages: list) -> None:
    payload = {"model": MODEL_NAME, "messages": messages, "stream": True}
    headers = {"Accept": "text/event-stream"}

    async with session.post("/v1/chat/completions", json=payload, headers=headers) as resp:
        resp.raise_for_status()
        async for raw in resp.content:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            chunk = (evt.get("choices") or [{}])[0].get("delta", {}).get("content")
            if chunk:
                print(chunk, end="", flush=True)
        print()
