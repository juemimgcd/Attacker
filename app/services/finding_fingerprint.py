import hashlib
import json


def finding_fingerprint(
    *,
    stage: str,
    case_id: str,
    category: str,
    is_control: bool,
) -> str:
    payload = {
        "case_id": case_id,
        "category": category,
        "is_control": is_control,
        "stage": stage,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
