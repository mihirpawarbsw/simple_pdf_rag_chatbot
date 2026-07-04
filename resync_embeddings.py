"""
resync_embeddings.py
=====================
Run this ONCE, right after applying the app.py / rag_logic.py fixes,
to repair the current broken state (files already listed as "uploaded"
in chat_history.db but with zero vectors in Chroma, because chroma_db/
got wiped/reset by the git pull conflict).

For every row in uploaded_files:
  1. Check whether the physical file still exists at its stored filepath.
  2. Check whether Chroma actually has vectors for (username, file_hash).
  3. If the file exists but vectors are missing -> re-ingest it.
  4. If the file itself is missing from disk -> report it (can't recover
     the content; the user will need to re-upload that one).

Usage:
    python resync_embeddings.py
"""

import os
import sqlite3

from rag_logic import ingest_document, is_already_embedded, compute_file_hash

DB_NAME = os.getenv("DB_NAME", "chat_history.db")


def main():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT username, session_id, filename, filepath, file_hash FROM uploaded_files"
    )
    rows = cursor.fetchall()
    conn.close()

    print(f"Found {len(rows)} uploaded_files row(s) to check.\n")

    fixed, missing_on_disk, already_ok, errors = [], [], [], []

    for username, session_id, filename, filepath, file_hash in rows:
        if not filepath or not os.path.exists(filepath):
            missing_on_disk.append((username, filename, filepath))
            continue

        # Recompute the hash from disk in case it drifted from the DB value
        try:
            current_hash = compute_file_hash(filepath)
        except Exception as e:
            errors.append((username, filename, str(e)))
            continue

        if is_already_embedded(current_hash, username):
            already_ok.append((username, filename))
            continue

        print(f"[Resync] Re-embedding {filename} (user={username}) — vectors missing from Chroma...")
        try:
            result = ingest_document(
                filepath, filename, username, session_id, DB_NAME,
                force_reindex=True
            )
            if result.get("status") == "success":
                fixed.append((username, filename, result.get("chunk_count", 0)))
            else:
                errors.append((username, filename, result))
        except Exception as e:
            errors.append((username, filename, str(e)))

    print("\n──────────── Resync summary ────────────")
    print(f"Already OK (had vectors):     {len(already_ok)}")
    print(f"Re-embedded successfully:     {len(fixed)}")
    for u, f, c in fixed:
        print(f"    + {f} (user={u}) -> {c} chunks")
    print(f"Missing from disk (need re-upload): {len(missing_on_disk)}")
    for u, f, p in missing_on_disk:
        print(f"    ! {f} (user={u}) expected at {p}")
    print(f"Errors:                        {len(errors)}")
    for u, f, e in errors:
        print(f"    x {f} (user={u}): {e}")


if __name__ == "__main__":
    main()
