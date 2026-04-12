"""Benchmark the hosted granite-docling VLM on Modal.

Measures:
    * number of running runners (containers) before and after
    * cold-start request time (first hit after containers are at 0)
    * warm request throughput: total wall time + per-request statistics
    * billed GPU-time (active time + configured scaledown window)
    * estimated cost in USD

Reads `benchmark_config.yaml` (next to this file by default). The YAML supports
`${VAR}` and `${VAR:-default}` env-variable expansion so values can be
overridden without editing the file.

Usage:
    python benchmarks/benchmark_vlm_modal.py
    python benchmarks/benchmark_vlm_modal.py -c path/to/config.yaml
    BENCH_WARM_REQUESTS=20 python benchmarks/benchmark_vlm_modal.py
"""

import argparse
import asyncio
import base64
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from statistics import mean, median, stdev

import aiohttp
import modal
import yaml

# --- Config loading ---------------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand_env(text: str) -> str:
    """Expand ${VAR} and ${VAR:-default} in a string."""

    def repl(match: re.Match) -> str:
        spec = match.group(1)
        if ":-" in spec:
            name, default = spec.split(":-", 1)
            return os.environ.get(name, default)
        return os.environ.get(spec, "")

    return _ENV_PATTERN.sub(repl, text)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config(path: Path) -> dict:
    raw = path.read_text()
    return yaml.safe_load(_expand_env(raw))


# --- Modal helpers ----------------------------------------------------------


def _serve_fn(cls_ref):
    """Return the deployed `serve` method ref on a Cls.from_name handle."""
    return cls_ref().serve


def get_runner_count(cls_ref) -> int | None:
    """Count active runners for the serve method. Returns None if unsupported."""
    try:
        stats = _serve_fn(cls_ref).get_current_stats()
    except Exception as exc:
        print(f"  (runner count unavailable: {exc})", file=sys.stderr)
        return None
    # Modal stats objects expose different attrs across versions; try a few.
    for attr in ("num_total_runners", "num_active_runners", "num_runners"):
        if hasattr(stats, attr):
            return int(getattr(stats, attr))
    return None


async def resolve_url(cls_ref) -> str:
    return await cls_ref().serve.get_web_url.aio()


async def wait_for_cold(cls_ref, poll_interval: float, max_wait: float) -> None:
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        n = get_runner_count(cls_ref)
        if n is None:
            print("  (cannot poll runner count — assuming cold)", file=sys.stderr)
            return
        if n == 0:
            return
        print(f"  {n} runner(s) still live, sleeping {poll_interval:.0f}s", file=sys.stderr)
        await asyncio.sleep(poll_interval)
    print("WARN: timed out waiting for runners to hit 0", file=sys.stderr)


# --- Request building -------------------------------------------------------


def build_image_part(image: str) -> dict:
    if image.startswith(("http://", "https://", "data:")):
        return {"type": "image_url", "image_url": {"url": image}}
    path = Path(image).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"image not found: {path}")
    mime, _ = mimetypes.guess_type(path.name)
    mime = mime or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


async def send_request(
    session: aiohttp.ClientSession,
    image_part: dict,
    prompt: str,
    model: str,
) -> tuple[float, int]:
    """Send one non-streaming chat completion. Return (elapsed_s, output_tokens)."""
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {
                "role": "user",
                "content": [image_part, {"type": "text", "text": prompt}],
            },
        ],
    }
    start = time.perf_counter()
    async with session.post("/v1/chat/completions", json=payload) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {body[:400]}")
        data = await resp.json()
    elapsed = time.perf_counter() - start
    output_tokens = int((data.get("usage") or {}).get("completion_tokens") or 0)
    return elapsed, output_tokens


# --- Benchmark --------------------------------------------------------------


async def run_benchmark(cfg: dict) -> None:
    modal_cfg = cfg["modal"]
    app_name = modal_cfg["app_name"]
    class_name = modal_cfg["class_name"]
    scaledown_s = float(modal_cfg["deployed_scaledown_window_s"])

    req_cfg = cfg["request"]
    image_src = req_cfg["image"]
    prompt = req_cfg["prompt"]
    req_timeout_s = float(req_cfg["request_timeout_s"])

    model_name = cfg["model"]["name"]

    bench_cfg = cfg["benchmark"]
    warm_n = int(bench_cfg["warm_requests"])
    concurrency = max(1, int(bench_cfg["concurrency"]))
    require_cold = _as_bool(bench_cfg["require_cold"])
    poll_interval = float(bench_cfg["poll_interval_s"])
    max_cold_wait = float(bench_cfg["max_cold_wait_s"])

    cost_cfg = cfg["cost"]
    gpu_label = cost_cfg["gpu"]
    gpu_price_per_hour = float(cost_cfg["gpu_price_per_hour"])
    include_scaledown = _as_bool(cost_cfg["include_scaledown"])

    cls = modal.Cls.from_name(app_name, class_name)

    baseline_runners = get_runner_count(cls)
    print(f"Runners at start: {baseline_runners}", file=sys.stderr)

    if require_cold and baseline_runners not in (None, 0):
        print("Waiting for containers to scale to 0...", file=sys.stderr)
        await wait_for_cold(cls, poll_interval, max_cold_wait)

    url = await resolve_url(cls)
    print(f"Endpoint: {url}\n", file=sys.stderr)

    image_part = build_image_part(image_src)
    http_timeout = aiohttp.ClientTimeout(total=req_timeout_s)

    async with aiohttp.ClientSession(base_url=url, timeout=http_timeout) as session:
        # --- Cold-start request --------------------------------------------
        print("--- Cold-start request ---", file=sys.stderr)
        cold_elapsed, cold_tokens = await send_request(
            session, image_part, prompt, model_name
        )
        print(f"Cold start: {cold_elapsed:.2f}s ({cold_tokens} tokens)", file=sys.stderr)

        # --- Warm requests -------------------------------------------------
        print(
            f"\n--- Warm requests: n={warm_n}, concurrency={concurrency} ---",
            file=sys.stderr,
        )
        warm_times: list[float] = []
        warm_tokens: list[int] = []
        warm_wall_start = time.perf_counter()

        if concurrency == 1:
            for i in range(warm_n):
                t, tk = await send_request(session, image_part, prompt, model_name)
                warm_times.append(t)
                warm_tokens.append(tk)
                print(f"  req {i + 1:>2}/{warm_n}: {t:.2f}s ({tk} tokens)", file=sys.stderr)
        else:
            sem = asyncio.Semaphore(concurrency)

            async def one():
                async with sem:
                    return await send_request(session, image_part, prompt, model_name)

            results = await asyncio.gather(*(one() for _ in range(warm_n)))
            warm_times = [r[0] for r in results]
            warm_tokens = [r[1] for r in results]

        warm_wall = time.perf_counter() - warm_wall_start

    # --- Aggregation --------------------------------------------------------
    final_runners = get_runner_count(cls)
    active_total_s = cold_elapsed + warm_wall
    spindown_s = scaledown_s if include_scaledown else 0.0
    billed_s = active_total_s + spindown_s
    cost_usd = billed_s / 3600.0 * gpu_price_per_hour
    total_tokens = cold_tokens + sum(warm_tokens)

    bar = "=" * 64
    print("\n" + bar)
    print("Benchmark Summary")
    print(bar)
    print(f"App / class:              {app_name} / {class_name}")
    print(f"Model:                    {model_name}")
    print(f"Runners (before → after): {baseline_runners} → {final_runners}")
    print()
    print(f"Cold start:               {cold_elapsed:>8.2f} s  ({cold_tokens} tokens)")
    print(f"Warm wall-clock (n={warm_n:>2}):  {warm_wall:>8.2f} s")
    if warm_times:
        sorted_times = sorted(warm_times)
        p95_idx = max(0, min(len(sorted_times) - 1, int(round(len(sorted_times) * 0.95)) - 1))
        print(f"  per-request mean:       {mean(warm_times):>8.2f} s")
        print(f"  per-request median:     {median(warm_times):>8.2f} s")
        if len(warm_times) > 1:
            print(f"  per-request stdev:      {stdev(warm_times):>8.2f} s")
        print(f"  per-request min / max:  {min(warm_times):>8.2f} s / {max(warm_times):.2f} s")
        print(f"  per-request p95:        {sorted_times[p95_idx]:>8.2f} s")
        if warm_wall > 0:
            print(f"  effective throughput:   {warm_n / warm_wall:>8.2f} req/s")
    print()
    print(f"Active total:             {active_total_s:>8.2f} s  (cold + warm wall)")
    scope = "included" if include_scaledown else "excluded"
    print(f"Scaledown (spindown):     {spindown_s:>8.2f} s  ({scope})")
    print(f"Billed total:             {billed_s:>8.2f} s")
    print()
    print(f"GPU rate:                 ${gpu_price_per_hour:.4f}/hour ({gpu_label})")
    print(f"Estimated cost:           ${cost_usd:.4f}")
    if total_tokens:
        print(f"Cost per 1k output tok:   ${cost_usd / total_tokens * 1000:.4f}")
    print(bar)


# --- CLI --------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path(__file__).parent / "benchmark_config.yaml",
        help="path to benchmark_config.yaml (default: alongside this script)",
    )
    args = parser.parse_args()
    cfg = load_config(args.config)
    asyncio.run(run_benchmark(cfg))


if __name__ == "__main__":
    main()
