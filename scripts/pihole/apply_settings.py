"""Print scripts/pihole/pihole.toml as `key<TAB>value` lines for
`pihole-FTL --config`, one per setting. Arrays go out as JSON, which is what
FTL's CLI parses them from; booleans as true/false.

Usage: apply_settings.py <rendered pihole.toml>
"""

import json
import sys
import tomllib


def flatten(table, prefix=""):
    for key, value in table.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from flatten(value, f"{path}.")
        elif isinstance(value, bool):
            yield path, "true" if value else "false"
        elif isinstance(value, list):
            yield path, json.dumps(value)
        else:
            yield path, str(value)


if __name__ == "__main__":
    with open(sys.argv[1], "rb") as f:
        for key, value in flatten(tomllib.load(f)):
            print(f"{key}\t{value}")
