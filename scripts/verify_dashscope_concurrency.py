"""Verify interactive /model switching against DashScope without persisting keys.

Set MINIAGENT_API_KEY_1 through MINIAGENT_API_KEY_4 before running.  Each
configured key gets five simultaneous CLI sessions.  The five calls cycle
through the requested Qwen models and assert both the /model acknowledgement
and a minimal online response.  Only aggregate pass/fail data is printed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


MODELS = ("qwen-max", "qwen3.6-plus", "qwen-3.6-flash",
          "qwen-max", "qwen3.6-plus")
MARKER = "MINIAGENT_ONLINE_OK"
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Case:
    key_slot: int
    model: str


def configured_keys() -> list[tuple[int, str]]:
    """Read slots without ever logging their values."""
    keys = [(slot, os.environ[f"MINIAGENT_API_KEY_{slot}"])
            for slot in range(1, 5)
            if os.environ.get(f"MINIAGENT_API_KEY_{slot}")]
    if not keys and os.environ.get("MINIAGENT_API_KEY"):
        keys.append((1, os.environ["MINIAGENT_API_KEY"]))
    return keys


def cases(keys: list[tuple[int, str]]) -> list[Case]:
    return [Case(slot, model) for slot, _ in keys for model in MODELS]


def run_case(case: Case, api_key: str) -> tuple[Case, bool, str]:
    env = os.environ.copy()
    env["MINIAGENT_API_KEY"] = api_key
    env.setdefault("MINIAGENT_BASE_URL",
                   "https://dashscope.aliyuncs.com/compatible-mode/v1")
    workdir = Path(tempfile.mkdtemp(prefix="miniagent-dashscope-"))
    try:
        command = f"/model {case.model}\nReply with exactly {MARKER}.\n/exit\n"
        result = subprocess.run(
            [sys.executable, "-m", "miniagent", "--workdir", str(workdir)],
            cwd=ROOT, env=env, input=command, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=330,
        )
        switched = f"模型已切换: {case.model}" in result.stdout
        replied = MARKER in result.stdout
        if result.returncode == 0 and switched and replied:
            return case, True, "ok"
        return case, False, (f"exit={result.returncode}, switched={switched}, "
                             f"marker={replied}")
    except subprocess.TimeoutExpired:
        return case, False, "timeout"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    keys = configured_keys()
    if not keys:
        print("No key slots configured. Set MINIAGENT_API_KEY_1..._4 first.")
        return 2

    key_by_slot = dict(keys)
    all_cases = cases(keys)
    print(f"Starting {len(all_cases)} CLI calls: 5 concurrent calls per key slot.")
    failures = []
    with ThreadPoolExecutor(max_workers=len(all_cases)) as pool:
        futures = [pool.submit(run_case, case, key_by_slot[case.key_slot])
                   for case in all_cases]
        for future in as_completed(futures):
            case, ok, detail = future.result()
            status = "PASS" if ok else "FAIL"
            print(f"{status} key-slot={case.key_slot} model={case.model} {detail}")
            if not ok:
                failures.append(case)
    print(f"Completed: {len(all_cases) - len(failures)}/{len(all_cases)} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
