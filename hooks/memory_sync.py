#!/usr/bin/env python3
"""Sync cloud memory to local SQLite + Chroma.

Usage:
    python3 tools/memory_sync.py [--workspace-id WS] [--db PATH] [--chroma PATH]
    python3 tools/memory_sync.py --status
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_API_URL = os.environ.get("MAYRING_API_URL", "https://mcp.linn.games").rstrip("/")
_JWT_FILE = os.path.expanduser("~/.config/mayring/hook.jwt")
_DEFAULT_DB = os.path.expanduser("~/.cache/mayringcoder/memory.db")

# Device↔Cloud-Kanal (#5): X-Device-Id auch auf den Sync-Calls.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _device import device_headers
except ImportError:
    def device_headers() -> dict:  # type: ignore[misc]
        return {}
_DEFAULT_CHROMA = os.path.expanduser("~/.cache/mayringcoder/chroma")
_DEFAULT_WS = os.environ.get("MAYRING_WORKSPACE_ID", "default")
# WHY(LAN-cutover): three.linn.games ist Legacy/offline — der Hook läuft lokal,
# also default localhost:11434 (eigene GPU). Nur _local_embed nutzt das (Fallback,
# wenn ein Cloud-Chunk kein Embedding mitbringt).
_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# WHY(bge-m3-migration): NICHT mehr hardcoded nomic (768d). Der Store ist auf bge-m3
# (1024d) migriert; lokal re-embeddete Chunks MÜSSEN dieselbe Dimension wie die
# Cloud-Embeddings haben, sonst sprengt der Upsert die lokale Collection.
_EMBED_MODEL = os.environ.get("MAYRING_EMBED_MODEL") or os.environ.get("EMBEDDING_MODEL") or "bge-m3"
_BATCH_SIZE = 200
_TIMEOUT = 30


def _read_token() -> str:
    try:
        return Path(_JWT_FILE).read_text().strip()
    except FileNotFoundError:
        return ""


def _init_local_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            text TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            text_hash TEXT,
            dedup_key TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_created ON chunks(created_at);
        CREATE TABLE IF NOT EXISTS sync_watermark (
            workspace_id TEXT PRIMARY KEY,
            cursor TEXT NOT NULL DEFAULT '2000-01-01T00:00:00'
        );
    """)
    conn.commit()
    return conn


def _get_cursor(conn: sqlite3.Connection, workspace_id: str) -> str:
    row = conn.execute(
        "SELECT cursor FROM sync_watermark WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()
    return row["cursor"] if row else "2000-01-01T00:00:00"


def _set_cursor(conn: sqlite3.Connection, workspace_id: str, cursor: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sync_watermark(workspace_id, cursor) VALUES(?,?)",
        (workspace_id, cursor),
    )
    conn.commit()


def _fetch_changes(token: str, workspace_id: str, since: str, limit: int = _BATCH_SIZE) -> dict:
    params = urllib.parse.urlencode({"since": since, "workspace_id": workspace_id, "limit": limit})
    req = urllib.request.Request(
        f"{_API_URL}/memory/changes?{params}",
        headers={"Authorization": f"Bearer {token}", **device_headers()},
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return json.loads(resp.read())


def _local_embed(text: str) -> list[float] | None:
    payload = json.dumps({"model": _EMBED_MODEL, "prompt": text}).encode()
    req = urllib.request.Request(
        f"{_OLLAMA_URL}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        return json.loads(resp.read()).get("embedding")
    except Exception:
        return None


def _upsert_chroma(chroma_path: str, chunks_with_embeddings: list[dict]) -> None:
    try:
        import chromadb  # type: ignore
    except ImportError:
        print("chromadb not installed — skipping Chroma sync", file=sys.stderr)
        return

    client = chromadb.PersistentClient(path=chroma_path)
    col = client.get_or_create_collection("memory_chunks")

    ids, embeddings, documents, metadatas = [], [], [], []
    for c in chunks_with_embeddings:
        if c["embedding"] is None:
            continue
        ids.append(c["chunk_id"])
        embeddings.append(c["embedding"])
        documents.append(c["text"])
        metadatas.append({
            "workspace_id": c["workspace_id"],
            "source_id": c["source_id"],
            "is_active": int(c["is_active"]),
        })

    if ids:
        try:
            col.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
        except Exception as exc:
            # WHY(bge-m3-migration): the local "memory_chunks" collection was created at
            # the old nomic dim (768); the cloud now serves bge-m3 (1024) embeddings →
            # "Collection expecting embedding with dimension of 768, got 1024" crashed
            # the UserPromptSubmit hook on every prompt. The local Chroma is a rebuildable
            # cache, so on a dimension mismatch drop + recreate the collection and retry
            # once. Self-heals any future embedding-model/dim change. Re-raise anything else.
            if "dimension" not in str(exc).lower():
                raise
            print(f"memory_sync: local Chroma dim mismatch ({exc}); recreating collection",
                  file=sys.stderr)
            client.delete_collection("memory_chunks")
            col = client.get_or_create_collection("memory_chunks")
            col.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)

    inactive = [c["chunk_id"] for c in chunks_with_embeddings if not c["is_active"]]
    if inactive:
        existing = col.get(ids=inactive, include=["metadatas"])
        for cid, meta in zip(existing["ids"], existing["metadatas"]):
            meta["is_active"] = 0
            col.update(ids=[cid], metadatas=[meta])


def _upsert_sqlite(conn: sqlite3.Connection, chunks: list[dict]) -> None:
    conn.executemany(
        """
        INSERT OR REPLACE INTO chunks
            (chunk_id, source_id, text, workspace_id, created_at, is_active, text_hash, dedup_key)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        [
            (
                c["chunk_id"], c["source_id"], c["text"], c["workspace_id"],
                c["created_at"], int(c["is_active"]), c.get("text_hash"), c.get("dedup_key"),
            )
            for c in chunks
        ],
    )
    conn.commit()


def sync(workspace_id: str, db_path: str, chroma_path: str) -> int:
    token = _read_token()
    if not token:
        print("No JWT found at", _JWT_FILE, file=sys.stderr)
        return 1

    conn = _init_local_db(db_path)
    cursor = _get_cursor(conn, workspace_id)

    total = 0
    while True:
        try:
            data = _fetch_changes(token, workspace_id, cursor, limit=_BATCH_SIZE)
        except Exception as e:
            print(f"Fetch error: {e}", file=sys.stderr)
            return 1

        chunks = data.get("chunks", [])
        if not chunks:
            break

        for c in chunks:
            if c["embedding"] is None and c["is_active"]:
                c["embedding"] = _local_embed(c["text"])

        _upsert_sqlite(conn, chunks)
        _upsert_chroma(chroma_path, chunks)

        new_cursor = data.get("cursor", cursor)
        _set_cursor(conn, workspace_id, new_cursor)
        total += len(chunks)
        cursor = new_cursor

        if len(chunks) < _BATCH_SIZE:
            break

    print(f"Synced {total} chunks (cursor: {cursor})")
    return 0


def status(workspace_id: str, db_path: str) -> None:
    if not Path(db_path).exists():
        print("No local DB found at", db_path)
        return
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor_row = conn.execute(
        "SELECT cursor FROM sync_watermark WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()
    count = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE workspace_id = ? AND is_active = 1", (workspace_id,)
    ).fetchone()[0]
    print(f"Workspace: {workspace_id}")
    print(f"Cursor:    {cursor_row['cursor'] if cursor_row else 'never synced'}")
    print(f"Chunks:    {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="MayringCoder memory sync")
    parser.add_argument("--workspace-id", default=_DEFAULT_WS)
    parser.add_argument("--db", default=_DEFAULT_DB)
    parser.add_argument("--chroma", default=_DEFAULT_CHROMA)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        status(args.workspace_id, args.db)
        return

    sys.exit(sync(args.workspace_id, args.db, args.chroma))


if __name__ == "__main__":
    main()
