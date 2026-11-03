"""Download the two pinned GGUF data files for the LM Studio image pilot.

Downloads stay in the workspace; import into LM Studio is a separate explicit step.
Range pieces are resumable and final SHA-256 values are verified before use.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import time

import httpx

ROOT = Path(__file__).resolve().parents[1] / "var" / "image-pilot" / "lmstudio"
REPO = "lmstudio-community/Qwen3-VL-4B-Instruct-GGUF"
MIRROR = "https://hf-mirror.com"
FILES = {
    "Qwen3-VL-4B-Instruct-Q4_K_M.gguf": (2497281568, "aa18af1fdda59081005319536eebff9406d556ce8a753d9a3aa81cf635eadde7"),
    "mmproj-Qwen3-VL-4B-Instruct-F16.gguf": (836180160, "1b70e983d69dd424eb16e0ed2104d0b2a58bf01d866657f4de5da6c963620f19"),
}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path = ROOT / "download_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        # Content is pinned by the known LFS SHA-256 values; mirror metadata endpoints
        # may redirect to an unreachable upstream, so no API request is required here.
        manifest = {"repository": REPO, "revision": "main", "content_pin": "SHA-256", "mirror": MIRROR,
                    "files": {name: {"bytes": size, "sha256": sha} for name, (size, sha) in FILES.items()}}
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    started = time.monotonic()
    for filename, (size, expected_hash) in FILES.items():
        target = ROOT / filename
        if target.exists() and target.stat().st_size == size and sha256(target) == expected_hash:
            print(f"Already verified: {filename}", flush=True)
            continue
        parts = ROOT / (filename + ".parts")
        parts.mkdir(exist_ok=True)
        block = 16 * 1024**2
        count = (size + block - 1) // block

        def fetch(index):
            start, end = index * block, min(size, (index + 1) * block)
            path = parts / f"{index:04}.part"
            if path.exists() and path.stat().st_size == end - start:
                return end - start
            temporary = path.with_suffix(".pending")
            for attempt in range(12):
                try:
                    offset = temporary.stat().st_size if temporary.exists() else 0
                    if offset == end - start:
                        temporary.replace(path)
                        return end - start
                    if offset > end - start:
                        raise ValueError("Oversized partial segment")
                    url = f"{MIRROR}/{REPO}/resolve/{manifest['revision']}/{filename}?download=true"
                    # Isolate connections so a failed TLS stream cannot exhaust a shared pool.
                    with httpx.stream("GET", url, headers={"Range": f"bytes={start + offset}-{end - 1}"},
                                      follow_redirects=True, timeout=httpx.Timeout(60, connect=15)) as response:
                        if response.status_code != 206:
                            raise ValueError(f"Range not honored: HTTP {response.status_code}")
                        if not response.headers.get("content-range", "").startswith(f"bytes {start + offset}-{end - 1}/"):
                            raise ValueError("Unexpected content range")
                        with temporary.open("ab") as out:
                            for chunk in response.iter_bytes(1024 * 1024):
                                out.write(chunk)
                    if temporary.stat().st_size != end - start:
                        raise ValueError("Incomplete segment")
                    temporary.replace(path)
                    return end - start
                except (httpx.HTTPError, ValueError) as exc:
                    print(f"Retry {filename} part {index}: {type(exc).__name__}: {str(exc)[:100]}", flush=True)
                    if attempt == 11:
                        raise
                    time.sleep(min(10, attempt + 1))

        cached = [i for i in range(count) if (parts / f"{i:04}.part").exists()
                  and (parts / f"{i:04}.part").stat().st_size == min(block, size - i * block)]
        completed = sum(min(block, size - i * block) for i in cached)
        print(f"Downloading {filename}: {size / 1024**3:.2f} GiB", flush=True)
        print(f"Resuming {100 * completed / size:.1f}% completed ranges", flush=True)
        failures = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            for future in as_completed([executor.submit(fetch, index) for index in range(count) if index not in cached]):
                try:
                    completed += future.result()
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    continue
                print(f"{filename}: {100 * completed / size:.1f}% ({completed // 1024**2} MiB), elapsed {time.monotonic() - started:.0f}s", flush=True)
        if failures:
            raise RuntimeError(f"{len(failures)} segments unavailable; verified ranges retained for retry")
        temporary = target.with_suffix(".assembling")
        with temporary.open("wb") as output:
            for index in range(count):
                with (parts / f"{index:04}.part").open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
        if sha256(temporary) != expected_hash:
            raise ValueError(f"Final SHA-256 failed: {filename}")
        temporary.replace(target)
        print(f"Verified: {filename}", flush=True)
    print("Both model files verified; ready for LM Studio import.", flush=True)


if __name__ == "__main__":
    main()
