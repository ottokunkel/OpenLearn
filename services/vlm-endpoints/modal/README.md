# vlm-granite-docling (Modal)

Modal app serving `ibm-granite/granite-docling-258M` via vLLM on an L40S, with
an OpenAI-compatible `/v1/chat/completions` endpoint.

Shared VLM endpoint — the doc-worker-v2 service (and anything else that needs
DocTags-emitting VLM output) points `VLM_ENDPOINT_URL` at this app.

---

## Deploy

```bash
cd services/vlm-endpoints/modal
uv run modal deploy src/vlm_modal/modal_app.py
```

Get the public URL and copy it into `services/doc-worker-v2/.env` as
`VLM_ENDPOINT_URL` (ending with `/v1/chat/completions`):

```bash
uv run modal app show vlm-granite-docling
# VLM_ENDPOINT_URL=https://<you>--vlm-granite-docling-vllmserver-serve.modal.run/v1/chat/completions
```

## Smoke test

Streams a transcription of a reference image against the deployed app:

```bash
uv run modal run src/vlm_modal/modal_app.py::test
```

The test runs twice — the first call exercises a cold start, the second hits
the warm snapshot.

## Configuration knobs

Edit the constants at the top of `src/vlm_modal/modal_app.py`:

| Constant            | Meaning                                                     |
| ------------------- | ----------------------------------------------------------- |
| `MODEL_NAME`        | HuggingFace model id served by vLLM.                        |
| `GPU`, `N_GPU`      | GPU type + count passed to `@app.cls(gpu=...)`.             |
| `MIN_CONTAINERS`    | 0 = fully cold-start; bump to 1 to keep a warm replica.     |
| `MAX_INPUTS`        | Concurrent requests per replica before Modal scales up.     |
| `SCALEDOWN_WINDOW`  | Seconds of idle before a container is scaled down.          |
| `MAX_MODEL_LEN`     | vLLM max sequence length (prompt + completion).             |

---

## Relationship to v1

`services/doc-worker-v1/src/vlm_endpoint/modal_app.py` is the original, under
`APP_NAME = "vlm-endpoint-docworker-v1"`. It stays deployed and v1 continues
pointing at it. This package is a verbatim lift with only `APP_NAME` changed
to `vlm-granite-docling`, so it's reusable by anything (not just v2).
