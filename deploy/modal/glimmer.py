"""Muse Glimmer 30B on Modal: vLLM's OpenAI server behind Modal's proxy auth, scaled to zero.

    uv run modal run deploy/modal/glimmer.py::download   # once: the weights into a volume
    uv run modal deploy deploy/modal/glimmer.py::app

Requests need a workspace proxy token as `Authorization: Bearer wk-<id>.ws-<secret>`; Modal
rejects others before a container starts. Set AIMS_GLIMMER_URL to the server's URL plus /v1
and AIMS_GLIMMER_KEY to the token for the `modal-glimmer` profile.
"""

import json
import subprocess

import modal

REPO = "meta-models/Muse-Glimmer-30B"
REVISION = "cec94fcfd6512f2cfe708138c32533b346ea4f69"
SERVED_NAME = "muse-glimmer"
WEIGHTS = "/weights"
MODEL_DIR = f"{WEIGHTS}/{REPO}/{REVISION}"
GPU = "H100"
PORT = 8000
MINUTES = 60

weights = modal.Volume.from_name("aims-glimmer-weights", create_if_missing=True)
compile_cache = modal.Volume.from_name("aims-glimmer-vllm-cache", create_if_missing=True)

download_image = modal.Image.debian_slim(python_version="3.12").uv_pip_install(
    "huggingface_hub[hf_xet]==2.4.0"
)
# Modal looks for `python` on PATH; the image only names it python3.
serve_image = modal.Image.from_registry(
    "vllm/vllm-openai:v0.32.0",
    setup_dockerfile_commands=["RUN ln -sf /usr/bin/python3 /usr/bin/python"],
).entrypoint([])

# The download is its own app, so it needs no GPU and no payment method.
weights_app = modal.App("aims-glimmer-weights")
app = modal.App("aims-glimmer")


@weights_app.function(image=download_image, volumes={WEIGHTS: weights}, timeout=60 * MINUTES)
def download() -> None:
    subprocess.run(
        ["hf", "download", REPO, "--revision", REVISION, "--local-dir", MODEL_DIR], check=True
    )
    weights.commit()


# No target_concurrency: one container at most, which vLLM batches for every agent of a run.
@app.server(
    image=serve_image,
    gpu=GPU,
    volumes={WEIGHTS: weights.read_only(), "/root/.cache/vllm": compile_cache},
    port=PORT,
    startup_timeout=20 * MINUTES,
    scaledown_window=5 * MINUTES,
)
class Server:
    @modal.enter()
    def start(self) -> None:
        cmd = [
            "vllm",
            "serve",
            MODEL_DIR,
            "--served-model-name",
            SERVED_NAME,
            "--host",
            "0.0.0.0",
            "--port",
            str(PORT),
            "--max-model-len",
            "131072",
            "--gpu-memory-utilization",
            "0.92",
            "--generation-config",
            "auto",
            "--enable-prompt-tokens-details",
            "--limit-mm-per-prompt",
            json.dumps({"image": 0, "video": 0}),
            "--enable-auto-tool-choice",
            "--tool-call-parser",
            "muse_glimmer",
            "--reasoning-parser",
            "muse_glimmer",
        ]
        print(*cmd)
        self.process = subprocess.Popen(cmd)

    @modal.exit()
    def stop(self) -> None:
        self.process.terminate()
