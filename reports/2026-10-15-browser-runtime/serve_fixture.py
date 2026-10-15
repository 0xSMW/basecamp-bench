import json
from pathlib import Path

from basecamp_bench.browser_runtime import FrontendServer
from basecamp_bench.safety import tree_manifest

folder = Path("reports/2026-10-15-browser-runtime").resolve()
root = folder / "fixture"
before = tree_manifest(root)
with FrontendServer(root) as server:
    (folder / "live.json").write_text(json.dumps({"url": server.url}, indent=2) + "\n")
    print(server.url, flush=True)
    input("Press Return only after Chrome checks finish: ")
    proof = server.evidence()
    proof["snapshot_unchanged"] = tree_manifest(root) == before
(folder / "server-proof.json").write_text(json.dumps(proof, indent=2) + "\n")
print("Server closed; snapshot unchanged:", proof["snapshot_unchanged"], flush=True)
