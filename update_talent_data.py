"""Rebuild the compact offline talent catalog from HeroesToolChest data (MIT)."""
import copy
import json
from pathlib import Path
from urllib.request import urlopen

BASE = "https://raw.githubusercontent.com/HeroesToolChest/heroes-data2/main/heroesdata/"


def fetch(path):
    with urlopen(BASE + path, timeout=60) as response:
        return json.load(response)


def apply_patch(document, operations):
    for op in operations:
        parts = [p.replace("~1", "/").replace("~0", "~") for p in op["path"].split("/")[1:]]
        parent = document
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = parts[-1] if parts else None
        kind = op["op"]
        if kind not in ("add", "remove", "replace", "test"):
            raise ValueError("Unsupported JSON patch operation: " + kind)
        if key is None:
            if kind in ("add", "replace"):
                document = copy.deepcopy(op["value"])
            else:
                raise ValueError("Unsupported root operation")
        elif isinstance(parent, list):
            index = len(parent) if key == "-" else int(key)
            if kind == "add":
                parent.insert(index, copy.deepcopy(op["value"]))
            elif kind == "remove":
                parent.pop(index)
            elif kind == "replace":
                parent[index] = copy.deepcopy(op["value"])
            elif parent[index] != op["value"]:
                raise ValueError("JSON patch test failed")
        elif kind == "remove":
            del parent[key]
        elif kind == "test":
            if parent[key] != op["value"]:
                raise ValueError("JSON patch test failed")
        else:
            parent[key] = copy.deepcopy(op["value"])
    return document


def main():
    versions = fetch(".version.json")["versions"]
    versions = [v for v in versions if "_ptr" not in v]
    version = versions[-1]
    cache = {}

    def load(version):
        if version in cache:
            return cache[version]
        meta = fetch(version + "/.hdp.json")
        data = fetch(version + "/data/" + meta["files"]["[data]"]["herodata"])
        if meta["json"] == "patch":
            data = apply_patch(copy.deepcopy(load(meta["depends-on"])), data)
        cache[version] = data
        return data

    for ver in versions:
        meta = fetch(ver + "/.hdp.json")
        if meta.get("extracted"):
            load(ver)
    catalogs = {}
    for ver, data in cache.items():
        heroes = {}
        for hero_id, hero in data["items"].items():
            talents = {}
            for tier, entries in hero.get("talents", {}).items():
                for talent in entries:
                    talents[talent["talentId"]] = [int(tier.lower().replace("level", "")), talent["sort"]]
            heroes[hero.get("unitId", "Hero" + hero_id)] = {
                "hyperlink_id": hero["hyperlinkId"], "talents": talents,
            }
        catalogs[str(int(ver.split(".")[-1].split("_")[0]))] = heroes
    output = {"source": BASE, "version": version, "builds": catalogs}
    Path(__file__).with_name("talent_data.json").write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print("Saved", len(catalogs), "builds; latest:", version)


if __name__ == "__main__":
    main()
