"""Poll Agent applog/sessions while a phone connects. Prints a line on new remote hits."""
from __future__ import annotations

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
SEEN: set[str] = set()


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


def key(entry: dict) -> str:
    return "|".join(str(entry.get(k, "")) for k in ("time", "method", "path", "remote", "status", "ms", "note", "error"))


def dump_snapshot() -> None:
    ping = get("/api/app/ping")
    sessions = get("/api/net/sessions")
    log = get("/api/net/applog")
    print("SNAPSHOT ping", json.dumps(ping, ensure_ascii=False))
    print("SNAPSHOT sessions", json.dumps(sessions, ensure_ascii=False)[:4000])
    entries = log.get("entries") or []
    print(f"SNAPSHOT applog_count={len(entries)}")
    for entry in entries:
        SEEN.add(key(entry))
        remote = entry.get("remote")
        tag = "PHONE" if remote and not str(remote).startswith("127.") else "LOCAL"
        print(f"SNAPSHOT {tag} {json.dumps(entry, ensure_ascii=False)}")


def loop() -> None:
    last_session_sig = ""
    dump_snapshot()
    print("WATCHING phone traffic on :8765")
    while True:
        time.sleep(2)
        try:
            ping = get("/api/app/ping")
            log = get("/api/net/applog")
            sessions = get("/api/net/sessions")
        except Exception as exc:
            print(f"WATCH_ERROR {type(exc).__name__}: {exc}")
            continue
        phone_hit = False
        for entry in log.get("entries") or []:
            k = key(entry)
            if k in SEEN:
                continue
            SEEN.add(k)
            remote = entry.get("remote")
            tag = "PHONE" if remote and not str(remote).startswith("127.") else "LOCAL"
            if tag != "PHONE":
                continue
            phone_hit = True
            print(f"{tag} {json.dumps(entry, ensure_ascii=False)} ping_sessions={ping.get('sessions')} robot={ping.get('robot_state')}")
        sig = json.dumps(sessions.get("sessions"), ensure_ascii=False, sort_keys=True)
        if phone_hit and sig != last_session_sig:
            last_session_sig = sig
            print(f"SESSIONS {sessions.get('count')} {sig[:1500]}")


if __name__ == "__main__":
    loop()
