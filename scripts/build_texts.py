"""Generate scenario_texts.yaml from the project's SCENARIO.md source."""

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
SCENARIO_PATHS = (ROOT / "SCENARIO.md", ROOT.parent / "SCENARIO.md")


def main() -> None:
    source = next((path for path in SCENARIO_PATHS if path.exists()), None)
    if source is None:
        raise FileNotFoundError(
            "SCENARIO.md must be in the bot project or its parent directory"
        )
    blocks = re.findall(
        r"```text id=(\S+)\r?\n(.*?)```",
        source.read_text(encoding="utf-8"),
        re.S,
    )
    data = {block_id: text.strip("\r\n") for block_id, text in blocks}
    output = ROOT / "content" / "scenario_texts.yaml"
    output.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=10**6),
        encoding="utf-8",
    )
    print(f"Записано {len(data)} текстов в {output}")


if __name__ == "__main__":
    main()
