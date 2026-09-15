"""Pure text-snapshot receipts. No correctness, origin or acceptance authority."""
from dataclasses import dataclass, field
import hashlib
import json
import re

MAX_RESULT_CHARS = 65536
MAX_SNAPSHOT_BYTES = 65536
MAX_PRIOR_RECEIPT_BYTES = 4096
DOMAIN_TAG = b"result_snapshot_receipt:v1\x00"
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HEX = re.compile(r"[0-9a-f]{64}")
_FIELDS = frozenset(("accepted", "encoding", "kind", "receipt_id", "schema_version", "snapshot_bytes", "snapshot_sha256", "source_scope", "task_id"))


@dataclass(frozen=True, slots=True, repr=False)
class Outcome:
    kind: str
    reason: str
    receipt: bytes | None = field(default=None, repr=False)
    snapshot: bytes | None = field(default=None, repr=False)
    accepted: bool = field(default=False, init=False)

    def __repr__(self):
        return f"Outcome(kind={self.kind!r}, reason={self.reason!r}, accepted=False)"


def _reject(reason):
    return Outcome("rejected", reason)


def _identity(value):
    return type(value) is str and 1 <= len(value) <= 64 and _ID.fullmatch(value) is not None


def _hex(value):
    return type(value) is str and len(value) == 64 and _HEX.fullmatch(value) is not None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _preimage(scope, task, digest, size):
    return {"encoding": "utf-8", "snapshot_bytes": size, "snapshot_sha256": digest, "source_scope": scope, "task_id": task}


def _receipt_id(payload):
    return hashlib.sha256(DOMAIN_TAG + _canonical(payload)).hexdigest()


def _encode_snapshot(text):
    return text.encode("utf-8", errors="strict")


def _decode_previous_utf8(raw):
    return raw.decode("utf-8", errors="strict")


def _unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate field")
        obj[key] = value
    return obj


def _invalid_number(_text):
    raise ValueError("Non-integer JSON number")


def _parse_previous_json(text):
    return json.loads(text, object_pairs_hook=_unique_pairs, parse_float=_invalid_number, parse_constant=_invalid_number)


def _validate_previous(raw, scope, task):
    """Caller already checked exact bytes and size, before any decoding."""
    try:
        if raw.startswith(b"\xef\xbb\xbf"):
            return None, "INVALID_PREVIOUS"
        value = _parse_previous_json(_decode_previous_utf8(raw))
        if type(value) is not dict or set(value) != _FIELDS:
            return None, "INVALID_PREVIOUS"
        if value["accepted"] is not False:
            return None, "INVALID_PREVIOUS"
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            return None, "INVALID_PREVIOUS"
        size = value["snapshot_bytes"]
        if type(size) is not int or not 1 <= size <= MAX_SNAPSHOT_BYTES:
            return None, "INVALID_PREVIOUS"
        if value["encoding"] != "utf-8" or value["kind"] != "result_snapshot_receipt":
            return None, "INVALID_PREVIOUS"
        if not _identity(value["source_scope"]) or not _identity(value["task_id"]):
            return None, "INVALID_PREVIOUS"
        if not _hex(value["snapshot_sha256"]) or not _hex(value["receipt_id"]):
            return None, "INVALID_PREVIOUS"
        if _canonical(value) != raw:
            return None, "INVALID_PREVIOUS"
        payload = _preimage(value["source_scope"], value["task_id"], value["snapshot_sha256"], size)
        if _receipt_id(payload) != value["receipt_id"]:
            return None, "INVALID_PREVIOUS"
        if value["source_scope"] != scope or value["task_id"] != task:
            return None, "PREVIOUS_IDENTITY_MISMATCH"
        return value, None
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None, "INVALID_PREVIOUS"


def project_result(response, *, expected_task_id, source_scope, previous=None):
    """Capture a bounded parsed response. All non-received values carry no bytes."""
    if not _identity(expected_task_id) or not _identity(source_scope):
        return _reject("INVALID_CONTEXT")
    if type(response) is not dict:
        return _reject("INVALID_RESPONSE")
    try:
        task, status, result, error = (response[k] for k in ("task_id", "status", "result", "error"))
    except KeyError:
        return _reject("INVALID_RESPONSE")
    if any(type(value) is not str for value in (task, status, result, error)):
        return _reject("INVALID_RESPONSE")
    if not _identity(task) or task != expected_task_id:
        return _reject("IDENTITY_MISMATCH")
    if len(status) > 9:
        return _reject("UNKNOWN_STATUS")
    if status in ("queued", "running"):
        return Outcome("pending", "PENDING")
    if status in ("failed", "cancelled"):
        return _reject("TERMINAL_FAILURE")
    if status != "completed":
        return _reject("UNKNOWN_STATUS")
    if error:
        return _reject("ERROR_CONFLICT")
    if not result:
        return _reject("EMPTY_RESULT")
    if len(result) > MAX_RESULT_CHARS:
        return _reject("RESULT_TOO_LARGE")

    old = None
    if previous is not None:
        if type(previous) is not bytes or not previous:
            return _reject("INVALID_PREVIOUS")
        if len(previous) > MAX_PRIOR_RECEIPT_BYTES:
            return _reject("PREVIOUS_TOO_LARGE")
        old, failure = _validate_previous(previous, source_scope, task)
        if failure is not None:
            return _reject(failure)

    try:
        snapshot = _encode_snapshot(result)
    except UnicodeEncodeError:
        return _reject("INVALID_UNICODE")
    size = len(snapshot)
    if size > MAX_SNAPSHOT_BYTES:
        return _reject("RESULT_TOO_LARGE")
    digest = hashlib.sha256(snapshot).hexdigest()
    if old is not None and (old["snapshot_sha256"], old["snapshot_bytes"]) != (digest, size):
        return Outcome("conflict", "CONTENT_CONFLICT")
    payload = _preimage(source_scope, task, digest, size)
    record = dict(payload, accepted=False, kind="result_snapshot_receipt", receipt_id=_receipt_id(payload), schema_version=1)
    receipt = _canonical(record)
    # Field bounds imply at most 436 bytes. Keep this invariant explicit.
    if len(receipt) > MAX_PRIOR_RECEIPT_BYTES:
        raise RuntimeError("Receipt invariant failed")
    return Outcome("received", "RECEIVED", receipt, snapshot)
