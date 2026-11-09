"""Creation dates, title protection, and confirmed DSH rename batch coordination.

The state machine is transport-agnostic. The DSH bridge performs the actual
write; this module decides whether a title is eligible, records protection,
holds a batch lease, throttles attempts, and verifies post-write titles.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TYPES = ("功能", "设计", "修复", "优化", "发布", "探索", "文档", "研究")
FORMATTED_TITLE = re.compile(r"^(?:\d{4}|\d{6})｜(?:" + "|".join(TYPES) + r")｜.+$")
TERMINAL = {"completed", "abandoned"}
BATCH_STATUSES = {"preview", "confirmed", "executing", "partial"} | TERMINAL
LEASE_SECONDS = 600


def parse_created_at(value, unit):
    if isinstance(value, bool) or value is None:
        raise ValueError("createdAt is missing or invalid")
    if unit == "iso":
        instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("ISO createdAt must include a timezone")
        instant = instant.astimezone(timezone.utc)
    elif unit == "auto":
        candidates = []
        for candidate_unit in ("seconds", "milliseconds"):
            try:
                candidates.append(parse_created_at(value, candidate_unit))
            except (ValueError, OverflowError, OSError):
                pass
        if len(candidates) != 1:
            raise ValueError("createdAt unit is ambiguous or out of range")
        return candidates[0]
    elif unit in ("seconds", "milliseconds"):
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise ValueError("createdAt must be finite")
        if unit == "milliseconds":
            timestamp /= 1000
        try:
            instant = datetime.fromtimestamp(timestamp, timezone.utc)
        except (ValueError, OverflowError, OSError) as error:
            raise ValueError("createdAt out of range; check --unit") from error
    else:
        raise ValueError("Unknown createdAt unit")
    if not 1990 <= instant.year <= 2100:
        raise ValueError("createdAt out of range; check --unit")
    return instant


def creation_date(value, unit, date_format="MMDD"):
    if date_format not in ("MMDD", "YYMMDD"):
        raise ValueError("Date format must be MMDD or YYMMDD")
    instant = parse_created_at(value, unit)
    try:
        shanghai = ZoneInfo("Asia/Shanghai")
    except ZoneInfoNotFoundError as error:
        if instant.year < 1992:
            raise ValueError("Historical dates require Asia/Shanghai timezone data") from error
        shanghai = timezone(timedelta(hours=8))
    pattern = "%m%d" if date_format == "MMDD" else "%y%m%d"
    return instant.astimezone(shanghai).strftime(pattern)


def thread_key(item):
    source = item.get("source")
    workspace = item.get("workspaceId") or item.get("hostId") or ""
    session_id = item.get("sessionId") or item.get("threadId")
    if source != "dsh" or not isinstance(session_id, str) or not session_id:
        raise ValueError("Each item requires source 'dsh' and sessionId")
    if not isinstance(workspace, str) or any("|" in part for part in (source, workspace, session_id)):
        raise ValueError("Invalid session identity")
    if not workspace:
        raise ValueError("DSH items require an explicit workspaceId")
    return f"dsh|{workspace}|{session_id}"


def preview_hash(items):
    mapping = [(item["key"], item["original_title"], item["proposed_title"]) for item in items]
    return hashlib.sha256(json.dumps(mapping, ensure_ascii=False).encode("utf-8")).hexdigest()


def read_memory(path):
    if not path.exists():
        return {"version": 3, "threads": {}, "batches": {}, "lease": None}
    memory = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(memory, dict) or memory.get("version") != 3:
        raise ValueError("Unrecognized DSH rename-chats state format")
    for section in ("threads", "batches"):
        if not isinstance(memory.get(section), dict):
            raise ValueError(f"Invalid state section: {section}")
        if any(not isinstance(record, dict) for record in memory[section].values()):
            raise ValueError(f"Invalid record in state section: {section}")
    if memory["version"] == 1:
        migrate_memory(memory)
    for record in memory["threads"].values():
        if "protected" in record and not isinstance(record["protected"], bool):
            raise ValueError("Invalid protection flag")
        if "last_applied_title" in record and not isinstance(record["last_applied_title"], str):
            raise ValueError("Invalid last_applied_title")
    for batch in memory["batches"].values():
        if batch.get("status") not in BATCH_STATUSES or not isinstance(batch.get("items"), list):
            raise ValueError("Invalid batch state")
        if not isinstance(batch.get("caller_id"), str) or not batch["caller_id"]:
            raise ValueError("Invalid batch caller_id")
        for timestamp in ("created_at", "updated_at"):
            if not isinstance(batch.get(timestamp), (int, float)) or not math.isfinite(batch[timestamp]):
                raise ValueError("Invalid batch timestamp")
        for item in batch["items"]:
            if not isinstance(item, dict) or item.get("key") != thread_key(item):
                raise ValueError("Invalid batch item identity")
            if not all(isinstance(item.get(field), str) for field in ("original_title", "proposed_title")):
                raise ValueError("Invalid batch item title")
            if item.get("status") not in {"planned", "pending", "succeeded", "skipped", "failed"}:
                raise ValueError("Invalid batch item status")
            if type(item.get("attempts")) is not int or item["attempts"] < 0:
                raise ValueError("Invalid batch item attempt count")
            if not isinstance(item.get("retryable"), bool):
                raise ValueError("Invalid batch item retry policy")
        if batch.get("preview_hash") != preview_hash(batch["items"]):
            raise ValueError("Preview mapping no longer matches its hash")
    if memory.get("lease") is not None and not isinstance(memory["lease"], dict):
        raise ValueError("Invalid execution lease")
    if memory.get("lease"):
        lease = memory["lease"]
        if lease.get("batch_id") not in memory["batches"] or not isinstance(lease.get("token"), str):
            raise ValueError("Invalid execution lease owner")
        for timestamp in ("expires_at", "not_before"):
            if not isinstance(lease.get(timestamp), (int, float)) or not math.isfinite(lease[timestamp]):
                raise ValueError("Invalid execution lease timestamp")
    return memory


def migrate_memory(memory):
    """Retain v1 protection and recoverable previews without assuming API success."""
    now = time.time()
    for caller_id, batch in memory["batches"].items():
        batch.setdefault("caller_id", caller_id)
        batch.setdefault("created_at", now)
        batch.setdefault("updated_at", now)
        batch.setdefault("items", [])
        for item in batch["items"]:
            item["key"] = thread_key(item)
            record = memory["threads"].get(item["key"], {})
            verified = record.get("last_applied_title") == item.get("proposed_title")
            attempted = record.get("pending_title") == item.get("proposed_title")
            status = "succeeded" if verified else ("pending" if attempted else "planned")
            item.setdefault("status", status)
            item.setdefault("attempts", int(attempted or verified))
            item.setdefault("retryable", True)
            item.setdefault("reason", "legacy")
            if item.get("original_title") == item.get("proposed_title"):
                item["status"] = "skipped"
        batch.setdefault("preview_hash", preview_hash(batch["items"]))
    memory["version"] = 2
    memory["lease"] = None


@contextmanager
def state_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # The lock file stays in place. Only the OS-held lock determines ownership.
    with path.with_name(path.name + ".lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as error:
            raise ValueError("State is busy; retry after the current write finishes") from error
        try:
            yield
        finally:
            release()


def write_memory(path, memory):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary_path = Path(handle.name)
            json.dump(memory, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def protect_record(memory, key, reason):
    record = memory["threads"].setdefault(key, {})
    record.update(protected=True, protected_reason=reason)


def proposed_title(item, date_format):
    kind = item.get("type") if isinstance(item.get("type"), str) else item.get("kind")
    topic = item.get("topic")
    if kind not in TYPES or not isinstance(topic, str) or not topic.strip():
        return item["original_title"], "insufficient_content"
    topic = topic.strip()
    if "｜" in topic or "\n" in topic or "\r" in topic:
        raise ValueError("Topic must be a single phrase without title separators")
    try:
        date = creation_date(item.get("createdAt"), item.get("unit", "milliseconds"), date_format)
    except (ValueError, OverflowError, OSError):
        return item["original_title"], "invalid_created_at"
    return f"{date}｜{kind}｜{topic}", ""


def prepare(memory, request):
    caller = request.get("caller_id")
    if not isinstance(caller, str) or not caller:
        raise ValueError("caller_id is required")
    if any(batch["caller_id"] == caller and batch["status"] not in TERMINAL for batch in memory["batches"].values()):
        raise ValueError("Resume or abandon the caller's existing batch first")
    inputs = request.get("items")
    if not isinstance(inputs, list) or len(inputs) > 40:
        raise ValueError("Prepare at most 40 items per batch")
    date_format = request.get("format", "MMDD")
    if date_format not in ("MMDD", "YYMMDD"):
        raise ValueError("Date format must be MMDD or YYMMDD")
    items, seen = [], set()
    for source in inputs:
        key = thread_key(source)
        original = source.get("original_title")
        if key in seen or not isinstance(original, str):
            raise ValueError("Items require unique identities and string original_title")
        seen.add(key)
        title, reason = proposed_title(source, date_format)
        record = memory["threads"].get(key, {})
        if record.get("protected"):
            reason = "protected"
        elif "last_applied_title" in record and original != record["last_applied_title"]:
            reason = "externally_changed"
            protect_record(memory, key, reason)
        elif source.get("titleSource") == "user" and original != title:
            reason = "suspected_manual"
            protect_record(memory, key, reason)
        elif "last_applied_title" not in record and original != title and FORMATTED_TITLE.match(original):
            reason = "suspected_manual"
            protect_record(memory, key, reason)
        if reason:
            title = original
        status = "skipped" if title == original else "planned"
        items.append({
            "key": key, "source": source["source"],
            "workspaceId": source.get("workspaceId") or source.get("hostId"),
            "sessionId": source.get("sessionId") or source.get("threadId"),
            "createdAt": source.get("createdAt"),
            "unit": source.get("unit", "milliseconds"), "original_title": original,
            "titleSource": source.get("titleSource"),
            "proposed_title": title, "status": status, "reason": reason or ("unchanged" if status == "skipped" else ""),
            "attempts": 0, "retryable": True,
        })
    batch_id = str(uuid.uuid4())
    now = time.time()
    batch = {"caller_id": caller, "status": "preview", "format": date_format,
             "created_at": now, "updated_at": now, "items": items, "preview_hash": preview_hash(items)}
    memory["batches"][batch_id] = batch
    return {"batch_id": batch_id, **batch}


def get_batch(memory, request):
    batch_id = request.get("batch_id")
    if batch_id not in memory["batches"]:
        raise ValueError("Unknown batch_id")
    return batch_id, memory["batches"][batch_id]


def confirm(memory, request):
    batch_id, batch = get_batch(memory, request)
    if batch["status"] != "preview" or request.get("preview_hash") != batch["preview_hash"]:
        raise ValueError("Confirmation requires the current preview and its hash")
    keys = request.get("keys", [item["key"] for item in batch["items"]])
    if not isinstance(keys, list) or set(keys) - {item["key"] for item in batch["items"]}:
        raise ValueError("Confirmation can only select displayed items")
    for item in batch["items"]:
        if item["key"] not in keys:
            item.update(status="skipped", reason="not_confirmed")
    batch.update(status="confirmed", updated_at=time.time())
    return {"batch_id": batch_id, "status": batch["status"]}


def claim(memory, request):
    batch_id, batch = get_batch(memory, request)
    if batch["status"] not in {"confirmed", "partial", "executing"}:
        raise ValueError("Only a confirmed or interrupted batch can execute")
    now = time.time()
    lease = memory.get("lease")
    if lease and lease["expires_at"] > now:
        if lease["batch_id"] != batch_id or lease["token"] != request.get("token"):
            raise ValueError("Another execution owns the lease")
    else:
        lease = {"batch_id": batch_id, "token": str(uuid.uuid4()), "not_before": now}
    lease["expires_at"] = now + LEASE_SECONDS
    memory["lease"] = lease
    batch.update(status="executing", updated_at=now)
    return dict(lease)


def owned_batch(memory, request):
    batch_id, batch = get_batch(memory, request)
    lease = memory.get("lease")
    if (not lease or lease["batch_id"] != batch_id or lease["token"] != request.get("token")
            or lease["expires_at"] <= time.time()):
        raise ValueError("Execution lease is absent, expired, or belongs to another owner")
    lease["expires_at"] = time.time() + LEASE_SECONDS
    return batch, lease


def get_item(batch, request):
    for item in batch["items"]:
        if item["key"] == request.get("key"):
            return item
    raise ValueError("Item is outside this batch")


def mark_success(memory, item, applied_title=None):
    actual = applied_title if isinstance(applied_title, str) else item["proposed_title"]
    item.update(status="succeeded", reason="verified" if actual == item["proposed_title"] else "verified_normalized",
                applied_title=actual)
    memory["threads"].setdefault(item["key"], {}).update(
        last_applied_title=actual, pending_title=None)


def begin(memory, request):
    batch, lease = owned_batch(memory, request)
    item = get_item(batch, request)
    current = request.get("current_title")
    if not isinstance(current, str):
        raise ValueError("begin requires a freshly read current_title")
    if item["status"] in {"succeeded", "skipped"}:
        return {"decision": "skip", "reason": item["status"]}
    if any(other["status"] == "pending" and other is not item for other in batch["items"]):
        raise ValueError("Resolve the pending API result before another write")
    record = memory["threads"].get(item["key"], {})
    if item["attempts"] and current in {item["proposed_title"], item.get("applied_title")}:
        mark_success(memory, item, item.get("applied_title"))
        return {"decision": "recovered"}
    changed_since_apply = "last_applied_title" in record and record["last_applied_title"] != current
    if record.get("protected") or changed_since_apply or current != item["original_title"]:
        item.update(status="skipped", reason="protected_or_changed")
        protect_record(memory, item["key"], item["reason"])
        return {"decision": "skip", "reason": item["reason"]}
    if item["attempts"] >= 2 or not item["retryable"]:
        item.update(status="failed", reason="retry_exhausted")
        return {"decision": "skip", "reason": item["reason"]}
    wait_seconds = max(0, lease["not_before"] - time.time())
    if wait_seconds:
        return {"decision": "wait", "seconds": wait_seconds}
    item.update(status="pending", attempts=item["attempts"] + 1)
    lease["not_before"] = time.time() + 0.5
    return {"decision": "write", "source": item["source"],
            "workspaceId": item["workspaceId"], "sessionId": item["sessionId"],
            "title": item["proposed_title"]}


def record_result(memory, request):
    batch, lease = owned_batch(memory, request)
    item = get_item(batch, request)
    if item["status"] != "pending":
        raise ValueError("Only a pending API attempt can be verified")
    delay = float(request.get("retry_after", 0))
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("retry_after must be a finite nonnegative number")
    lease["not_before"] = time.time() + max(0.5, delay)
    current = request.get("current_title")
    accepted = request.get("accepted_title")
    if accepted is not None and not isinstance(accepted, str):
        raise ValueError("accepted_title must be a string when provided")
    if current is None:
        item["reason"] = "verification_unavailable"
    elif current == item["proposed_title"]:
        mark_success(memory, item)
    elif accepted is not None and current == accepted:
        mark_success(memory, item, accepted)
    elif current == item["original_title"]:
        retryable = request.get("retryable", False) is True
        item.update(status="failed", reason="write_unverified", retryable=retryable)
        if retryable:
            lease["not_before"] = time.time() + max(2, delay)
    elif isinstance(current, str):
        item.update(status="skipped", reason="changed_during_write")
        protect_record(memory, item["key"], item["reason"])
    else:
        raise ValueError("current_title must be a string or null")
    batch["updated_at"] = time.time()
    return {"status": item["status"], "attempts": item["attempts"], "reason": item["reason"]}


def finish(memory, request):
    batch, _ = owned_batch(memory, request)
    complete = all(
        item["status"] in {"succeeded", "skipped"}
        or (item["status"] == "failed" and (item["attempts"] >= 2 or not item["retryable"]))
        for item in batch["items"])
    batch.update(status="completed" if complete else "partial", updated_at=time.time())
    memory["lease"] = None
    return {"status": batch["status"], "counts": {
        status: sum(item["status"] == status for item in batch["items"])
        for status in ("succeeded", "skipped", "failed", "pending", "planned")}}


def protect(memory, request):
    key = thread_key(request)
    protected = request.get("protected", True)
    if not isinstance(protected, bool):
        raise ValueError("protected must be boolean")
    record = memory["threads"].setdefault(key, {})
    if protected:
        protect_record(memory, key, "user_requested")
    else:
        current = request.get("current_title")
        if not isinstance(current, str):
            raise ValueError("Unprotect requires the current title")
        record.update(protected=False, protected_reason="", last_applied_title=current)
    return {"key": key, "protected": protected}


def abandon(memory, request):
    batch_id, batch = get_batch(memory, request)
    lease = memory.get("lease")
    if lease and lease["batch_id"] == batch_id and lease["expires_at"] > time.time():
        owned_batch(memory, request)
    if batch["status"] == "completed":
        raise ValueError("A completed batch is already final")
    batch.update(status="abandoned", updated_at=time.time())
    if lease and lease["batch_id"] == batch_id:
        memory["lease"] = None
    return {"status": "abandoned"}


def prune(memory, request):
    cutoff = time.time() - 90 * 86400
    terminal = sorted(
        ((key, batch) for key, batch in memory["batches"].items() if batch["status"] in TERMINAL),
        key=lambda pair: pair[1]["updated_at"], reverse=True)
    removed = []
    for index, (key, batch) in enumerate(terminal):
        if index >= 20 or batch["updated_at"] < cutoff:
            del memory["batches"][key]
            removed.append(key)
    return {"removed_batches": removed}


ACTIONS = {"prepare": prepare, "confirm": confirm, "claim": claim, "begin": begin,
           "record": record_result, "finish": finish, "protect": protect,
           "abandon": abandon, "prune": prune}


def run_action(path, request):
    if not isinstance(request, dict) or request.get("action") not in ACTIONS:
        raise ValueError("Unknown or missing action")
    with state_lock(path):
        memory = read_memory(path)
        result = ACTIONS[request["action"]](memory, request)
        prune(memory, {})
        write_memory(path, memory)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    parser.add_argument("--state-file", type=Path, default=root / "rename-chats" / "state.json")
    commands = parser.add_subparsers(dest="command", required=True)
    date = commands.add_parser("date")
    date.add_argument("created_at")
    date.add_argument("--unit", choices=("auto", "seconds", "milliseconds", "iso"), default="auto")
    date.add_argument("--format", choices=("MMDD", "YYMMDD"), default="MMDD")
    commands.add_parser("read")
    run = commands.add_parser("run")
    run.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "date":
            print(creation_date(args.created_at, args.unit, args.format))
        elif args.command == "read":
            print(json.dumps(read_memory(args.state_file), ensure_ascii=False, indent=2))
        else:
            request = json.loads(args.input.read_text(encoding="utf-8-sig"))
            print(json.dumps(run_action(args.state_file, request), ensure_ascii=False, indent=2))
    except (OSError, ValueError, OverflowError, KeyError, TypeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
