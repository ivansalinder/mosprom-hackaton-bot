"""Проверка: все тексты из SCENARIO.md есть в content/*.yaml дословно.

Запуск: uv run python scripts/check_scenario_texts.py
Код возврата 0 = всё совпало, 1 = есть расхождения.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_PATHS = (ROOT / "SCENARIO.md", ROOT.parent / "SCENARIO.md")
SKIP_IDS: set[str] = set()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def flatten(obj, out: list[str]) -> None:
    if isinstance(obj, str):
        out.append(norm(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            flatten(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            flatten(v, out)


def main() -> int:
    scenario_path = next((path for path in SCENARIO_PATHS if path.exists()), None)
    if scenario_path is None:
        print("SCENARIO.md must be in the bot project or its parent directory")
        return 1
    scenario = scenario_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```text id=(\S+)\n(.*?)```", scenario, re.S)
    strings: list[str] = []
    for f in sorted((ROOT / "content").glob("*.yaml")):
        flatten(yaml.safe_load(f.read_text(encoding="utf-8")), strings)
    haystack = " || ".join(strings)
    missing = []
    for block_id, text in blocks:
        if block_id in SKIP_IDS:
            continue
        if norm(text) not in haystack:
            missing.append(block_id)
    print(f"Блоков в SCENARIO.md: {len(blocks)}; не найдено в content/*.yaml: {len(missing)}")
    for block_id in missing:
        print("  НЕТ:", block_id)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
