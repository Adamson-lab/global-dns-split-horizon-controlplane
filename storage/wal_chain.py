from dataclasses import dataclass
import hashlib
import json
from typing import Any


@dataclass(frozen=True)
class WalEntry:
    seq: int
    prev_hash: str
    op_type: str
    fencing_token: int
    payload_json: str
    entry_hash: str


def chain_digest(seq: int, prev_hash: str, op_type: str, fencing_token: int, payload_json: str) -> str:
    raw = f"{seq}|{prev_hash}|{op_type}|{fencing_token}|{payload_json}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class HashChainedWal:
    def __init__(self) -> None:
        self._entries: list[WalEntry] = []

    def append(self, op_type: str, fencing_token: int, payload: dict[str, Any]) -> WalEntry:
        seq = len(self._entries) + 1
        prev_hash = self._entries[-1].entry_hash if self._entries else "0" * 64
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        entry_hash = chain_digest(seq, prev_hash, op_type, fencing_token, payload_json)
        entry = WalEntry(
            seq=seq,
            prev_hash=prev_hash,
            op_type=op_type,
            fencing_token=fencing_token,
            payload_json=payload_json,
            entry_hash=entry_hash,
        )
        self._entries.append(entry)
        return entry

    def verify(self) -> tuple[bool, str]:
        prev = "0" * 64
        for idx, e in enumerate(self._entries, start=1):
            if e.seq != idx:
                return False, f"sequence mismatch at {idx}"
            if e.prev_hash != prev:
                return False, f"prev_hash mismatch at seq {e.seq}"
            expected = chain_digest(e.seq, e.prev_hash, e.op_type, e.fencing_token, e.payload_json)
            if expected != e.entry_hash:
                return False, f"digest mismatch at seq {e.seq}"
            prev = e.entry_hash
        return True, "ok"

    def entries(self) -> list[WalEntry]:
        return list(self._entries)
