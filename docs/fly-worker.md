# Fly Worker Deploy

This repo can run as a private OMR worker for `admisioncr`.

## What It Exposes

- `GET /healthz`
- `GET /templates`
- `POST /process`

The worker currently ships with one template:

- `acr_v1` -> `inputs/hojaDeRespuestas_ACR`

## Request Format

Use exactly one of `object_key` or `local_path`.

```json
{
  "sheet_id": "sheet_123",
  "template_id": "acr_v1",
  "object_key": "incoming/session_1/sheet_123.pdf"
}
```

Local debug example:

```json
{
  "sheet_id": "debug_001",
  "template_id": "acr_v1",
  "local_path": "/app/inputs/hojaDeRespuestas_ACR/scans/josias_dpi300.pdf"
}
```

## Response Format

```json
{
  "sheet_id": "sheet_123",
  "template_id": "acr_v1",
  "status": "processed",
  "page_count": 1,
  "pages": [
    {
      "file_id": "sheet_123.png",
      "input_path": "scans/sheet_123.pdf#page=1",
      "answers": {
        "P1": "A",
        "P2": "C"
      },
      "blank_count": 0,
      "multimarked_fields": [],
      "score": null,
      "overlay_key": "overlays/sheet_123/sheet_123.png",
      "overlay_path": null
    }
  ],
  "local_artifact_dir": null,
  "result_key": "results/sheet_123.json",
  "artifact_keys": {
    "results_csv": "artifacts/sheet_123/Results_01PM.csv"
  }
}
```

## Required Secrets

Set these in Fly for the worker app:

- `OMR_S3_BUCKET`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION`

Optional:

- `OMR_S3_ENDPOINT_URL`
- `AWS_ENDPOINT_URL_S3`
- `OMR_API_TOKEN`

For Tigris on Fly, `AWS_REGION=auto` is acceptable.

## Local Run

```bash
source .venv/bin/activate
python -m uvicorn api:app --host 127.0.0.1 --port 8080
```

Example request:

```bash
curl -X POST http://127.0.0.1:8080/process \
  -H 'Content-Type: application/json' \
  --data '{
    "sheet_id": "debug_001",
    "template_id": "acr_v1",
    "local_path": "/home/ebadilla10/OMRChecker/inputs/hojaDeRespuestas_ACR/scans/josias_dpi300.pdf"
  }'
```

## Fly Launch

From this repo:

```bash
fly launch --no-deploy
fly secrets set OMR_S3_BUCKET=admisioncr-omr-prod
fly secrets set AWS_ACCESS_KEY_ID=xxx AWS_SECRET_ACCESS_KEY=xxx AWS_REGION=auto
fly secrets set OMR_API_TOKEN=change-me
fly deploy
```

The current `fly.toml` keeps the app private-only. `admisioncr-web` should call it over Fly private networking.

## Internal Call From admisioncr

Use the worker's internal hostname:

```text
http://admisioncr-omr.internal:8080/process
```

Example payload:

```json
{
  "sheet_id": "sheet_123",
  "template_id": "acr_v1",
  "object_key": "incoming/session_1/sheet_123.pdf"
}
```

If `OMR_API_TOKEN` is configured, send:

```text
Authorization: Bearer <token>
```
