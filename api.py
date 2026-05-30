import csv
import json
import os
import shutil
import tempfile
from pathlib import Path
from threading import Lock
from typing import Optional

import boto3
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from src.entry import entry_point
from src.logger import logger

APP_ROOT = Path(__file__).resolve().parent
PROCESS_LOCK = Lock()
TEMPLATE_REGISTRY = {
    "acr_v1": APP_ROOT / "inputs" / "hojaDeRespuestas_ACR",
}
TEMPLATE_BUNDLE_FILES = (
    "template.json",
    "config.json",
    "reference.png",
    "evaluation.json",
)


class ProcessRequest(BaseModel):
    sheet_id: str
    template_id: str = "acr_v1"
    object_key: Optional[str] = None
    local_path: Optional[str] = None
    overlay_prefix: str = "overlays"
    result_prefix: str = "results"
    artifact_prefix: str = "artifacts"


app = FastAPI(title="admisioncr-omr", version="0.1.0")


def require_internal_token(authorization: Optional[str]):
    expected_token = os.environ.get("OMR_API_TOKEN")
    if not expected_token:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = authorization.split(" ", 1)
    token = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else authorization
    if token != expected_token:
        raise HTTPException(status_code=401, detail="Invalid API token")


def get_template_dir(template_id: str) -> Path:
    template_dir = TEMPLATE_REGISTRY.get(template_id)
    if template_dir is None:
        raise HTTPException(status_code=404, detail=f"Unknown template_id: {template_id}")
    if not template_dir.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Configured template directory is missing: {template_dir}",
        )
    return template_dir


def get_s3_bucket() -> str:
    bucket = os.environ.get("OMR_S3_BUCKET") or os.environ.get("BUCKET_NAME")
    if not bucket:
        raise HTTPException(
            status_code=500,
            detail="Neither OMR_S3_BUCKET nor BUCKET_NAME is configured",
        )
    return bucket


def get_s3_client():
    endpoint_url = os.environ.get("OMR_S3_ENDPOINT_URL") or os.environ.get(
        "AWS_ENDPOINT_URL_S3"
    )
    region_name = os.environ.get("AWS_REGION", "auto")
    session = boto3.session.Session()
    return session.client("s3", endpoint_url=endpoint_url, region_name=region_name)


def stage_template_bundle(job_input_dir: Path, template_dir: Path):
    for bundle_file in TEMPLATE_BUNDLE_FILES:
        source = template_dir / bundle_file
        if source.exists():
            shutil.copy2(source, job_input_dir / bundle_file)


def stage_input_file(job_scans_dir: Path, request: ProcessRequest) -> Path:
    if bool(request.object_key) == bool(request.local_path):
        raise HTTPException(
            status_code=422,
            detail="Provide exactly one of object_key or local_path",
        )

    if request.local_path:
        local_path = Path(request.local_path).expanduser().resolve()
        if not local_path.exists():
            raise HTTPException(status_code=404, detail=f"local_path not found: {local_path}")
        destination = job_scans_dir / local_path.name
        shutil.copy2(local_path, destination)
        return destination

    object_key = request.object_key
    bucket = get_s3_bucket()
    destination = job_scans_dir / Path(object_key).name
    get_s3_client().download_file(bucket, object_key, str(destination))
    return destination


def run_omr_job(job_input_dir: Path, job_output_dir: Path):
    args = {
        "output_dir": str(job_output_dir),
        "setLayout": False,
    }
    with PROCESS_LOCK:
        entry_point(job_input_dir, args)


def find_results_csv(job_output_dir: Path) -> Path:
    csv_candidates = sorted(job_output_dir.rglob("Results_*.csv"))
    if not csv_candidates:
        raise HTTPException(status_code=500, detail="No results CSV was generated")
    return csv_candidates[0]


def parse_results(job_output_dir: Path):
    results_csv = find_results_csv(job_output_dir)
    checked_dir = results_csv.parent.parent / "CheckedOMRs"
    rows = list(csv.DictReader(results_csv.open()))
    if not rows:
        raise HTTPException(status_code=500, detail="The results CSV is empty")

    pages = []
    for row in rows:
        answers = {key: value for key, value in row.items() if key.startswith("P")}
        multimarked_fields = [key for key, value in answers.items() if len(value) > 1]
        blank_count = sum(1 for value in answers.values() if not value)
        score_text = row.get("score", "")
        try:
            score = float(score_text) if score_text not in ("", "NA") else None
        except ValueError:
            score = None

        overlay_path = checked_dir / row["file_id"]
        pages.append(
            {
                "file_id": row["file_id"],
                "input_path": row["input_path"],
                "answers": answers,
                "blank_count": blank_count,
                "multimarked_fields": multimarked_fields,
                "score": score,
                "overlay_path": str(overlay_path) if overlay_path.exists() else None,
            }
        )

    return {
        "results_csv": results_csv,
        "manual_dir": results_csv.parent.parent / "Manual",
        "pages": pages,
    }


def persist_local_artifacts(job_output_dir: Path, sheet_id: str):
    persisted_dir = APP_ROOT / "outputs" / "api_runs" / sheet_id
    if persisted_dir.exists():
        shutil.rmtree(persisted_dir)
    persisted_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(job_output_dir, persisted_dir)
    return persisted_dir


def upload_artifacts(request: ProcessRequest, processed_payload: dict, response_payload: dict):
    if not request.object_key:
        return {
            "result_key": None,
            "artifact_keys": {},
        }

    bucket = get_s3_bucket()
    client = get_s3_client()
    pages = processed_payload["pages"]

    overlay_prefix = request.overlay_prefix.strip("/")
    result_prefix = request.result_prefix.strip("/")
    artifact_prefix = request.artifact_prefix.strip("/")

    artifact_keys = {}
    for page in pages:
        overlay_path = page["overlay_path"]
        if overlay_path is None:
            page["overlay_key"] = None
            continue
        overlay_key = f"{overlay_prefix}/{request.sheet_id}/{page['file_id']}"
        client.upload_file(overlay_path, bucket, overlay_key)
        page["overlay_key"] = overlay_key
        page["overlay_path"] = None

    results_csv = processed_payload["results_csv"]
    results_csv_key = f"{artifact_prefix}/{request.sheet_id}/{results_csv.name}"
    client.upload_file(str(results_csv), bucket, results_csv_key)
    artifact_keys["results_csv"] = results_csv_key

    manual_dir = processed_payload["manual_dir"]
    for file_name in ("MultiMarkedFiles.csv", "ErrorFiles.csv"):
        manual_file = manual_dir / file_name
        if manual_file.exists():
            artifact_key = f"{artifact_prefix}/{request.sheet_id}/{file_name}"
            client.upload_file(str(manual_file), bucket, artifact_key)
            artifact_keys[file_name] = artifact_key

    result_key = f"{result_prefix}/{request.sheet_id}.json"
    payload_bytes = json.dumps(response_payload, ensure_ascii=False).encode("utf-8")
    client.put_object(
        Bucket=bucket,
        Key=result_key,
        Body=payload_bytes,
        ContentType="application/json",
    )

    return {
        "result_key": result_key,
        "artifact_keys": artifact_keys,
    }


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "templates": sorted(TEMPLATE_REGISTRY.keys()),
        "s3_configured": bool(
            os.environ.get("OMR_S3_BUCKET") or os.environ.get("BUCKET_NAME")
        ),
    }


@app.get("/templates")
def list_templates():
    return {
        "templates": [
            {"template_id": template_id, "path": str(template_dir)}
            for template_id, template_dir in TEMPLATE_REGISTRY.items()
        ]
    }


@app.post("/process")
def process_sheet(request: ProcessRequest, authorization: Optional[str] = Header(default=None)):
    require_internal_token(authorization)
    template_dir = get_template_dir(request.template_id)

    with tempfile.TemporaryDirectory(prefix="admisioncr-omr-") as temp_dir:
        temp_root = Path(temp_dir)
        job_input_dir = temp_root / "input"
        job_output_dir = temp_root / "output"
        job_scans_dir = job_input_dir / "scans"
        job_scans_dir.mkdir(parents=True, exist_ok=True)
        stage_template_bundle(job_input_dir, template_dir)
        staged_input = stage_input_file(job_scans_dir, request)

        logger.info(
            f"Processing sheet_id='{request.sheet_id}' template='{request.template_id}' source='{staged_input.name}'"
        )
        run_omr_job(job_input_dir, job_output_dir)
        processed_payload = parse_results(job_output_dir)
        pages = processed_payload["pages"]
        needs_review = any(page["multimarked_fields"] for page in pages)
        local_artifact_dir = None
        if not request.object_key:
            local_artifact_dir = persist_local_artifacts(job_output_dir, request.sheet_id)
            for page in pages:
                if page["overlay_path"] is not None:
                    page["overlay_path"] = str(
                        local_artifact_dir / "scans" / "CheckedOMRs" / page["file_id"]
                    )

        response_payload = {
            "sheet_id": request.sheet_id,
            "template_id": request.template_id,
            "status": "needs_review" if needs_review else "processed",
            "page_count": len(pages),
            "pages": pages,
            "local_artifact_dir": str(local_artifact_dir) if local_artifact_dir else None,
        }
        storage_info = upload_artifacts(request, processed_payload, response_payload)
        response_payload["result_key"] = storage_info["result_key"]
        response_payload["artifact_keys"] = storage_info["artifact_keys"]

        return response_payload
