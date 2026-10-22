#!/usr/bin/env python3
import argparse
import copy
import json
import os
import re
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

LOCAL_TIMEZONE = "Asia/Baghdad"

APP_FIELD_ORDER = [
    "id",
    "name",
    "bundleIdentifier",
    "bundleId",
    "developerName",
    "iconURL",
    "localizedDescription",
    "category",
    "fileType",
    "recommended",
    "toolVersion",
    "versions",
    "version",
    "versionDate",
    "downloadURL",
    "ipaUrl",
    "size",
    "addedAt",
    "updatedAt",
]

DATE_FIELDS = ("versionDate", "addedAt", "updatedAt")
OPTIONAL_FIELDS = ("recommended", "toolVersion")
RELEASE_URL_FIELDS = ("downloadURL", "ipaUrl")


def today_string():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(LOCAL_TIMEZONE)).strftime("%Y-%m-%d")
        except Exception:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def clean_text(value):
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"[\u200b\u200c\u200d\ufeff\u2060]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_text_keep_lines(value):
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"[\u200b\u200c\u200d\ufeff\u2060]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_empty(value):
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"", "null", "none", "nan"}
    return False


def first_non_empty(*values):
    for value in values:
        if not is_empty(value):
            return value
    return None


def normalize_date(value):
    value = first_non_empty(value)
    if value is None:
        return None
    text = str(value).strip()
    if "T" in text:
        text = text.split("T", 1)[0].strip()
    return text or None


def size_to_bytes(size):
    if size is None:
        return 0
    if isinstance(size, bool):
        return int(size)
    if isinstance(size, int):
        return size
    if isinstance(size, float):
        return int(size)

    text = str(size).strip().lower().replace(",", "")
    if not text:
        return 0
    if text.isdigit():
        return int(text)

    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return 0

    value = float(match.group(1))
    if any(unit in text for unit in ("gb", "gib", "جيجا", "غيغا")):
        return int(value * 1024 * 1024 * 1024)
    if any(unit in text for unit in ("mb", "mib", "ميجا", "ميكا", "مب")):
        return int(value * 1024 * 1024)
    if any(unit in text for unit in ("kb", "kib", "كيلو", "كب")):
        return int(value * 1024)
    return int(value)


def order_dict(data, preferred_order):
    ordered = {key: data[key] for key in preferred_order if key in data}
    for key, value in data.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def load_json(path, required=True):
    if not os.path.exists(path):
        if required:
            raise FileNotFoundError(path)
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
        raise ValueError(f"{path} must contain an object with apps[]")
    return data


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def build_versions(app):
    versions = []
    current_version = clean_text(app.get("version"))
    current_url = first_non_empty(app.get("downloadURL"), app.get("ipaUrl"))
    current_date = normalize_date(
        first_non_empty(app.get("versionDate"), app.get("updatedAt"), app.get("addedAt"))
    )
    current_size = size_to_bytes(app.get("size"))

    if current_version and not is_empty(current_url):
        item = {
            "version": current_version,
            "downloadURL": clean_text(current_url),
        }
        if current_date:
            item["date"] = current_date
        if current_size > 0:
            item["size"] = current_size
        versions.append(item)

    previous = app.get("previousVersions")
    if isinstance(previous, list):
        for old in previous:
            if len(versions) >= 2:
                break
            if not isinstance(old, dict):
                continue
            old_version = clean_text(old.get("version"))
            old_url = first_non_empty(old.get("downloadURL"), old.get("ipaUrl"))
            if not old_version or is_empty(old_url):
                continue
            if any(clean_text(v.get("version")) == old_version for v in versions):
                continue
            item = {
                "version": old_version,
                "downloadURL": clean_text(old_url),
            }
            old_date = normalize_date(
                first_non_empty(old.get("date"), old.get("versionDate"), old.get("updatedAt"), old.get("addedAt"))
            )
            old_size = size_to_bytes(old.get("size"))
            if old_date:
                item["date"] = old_date
            if old_size > 0:
                item["size"] = old_size
            versions.append(item)

    return versions[:2]


def normalize_app(raw):
    app = copy.deepcopy(raw)

    if "name" in app:
        app["name"] = clean_text(app.get("name"))

    if is_empty(app.get("downloadURL")) and not is_empty(app.get("ipaUrl")):
        app["downloadURL"] = clean_text(app.get("ipaUrl"))
    if is_empty(app.get("ipaUrl")) and not is_empty(app.get("downloadURL")):
        app["ipaUrl"] = clean_text(app.get("downloadURL"))

    icon = first_non_empty(app.get("iconURL"), app.get("icon"))
    if not is_empty(icon):
        app["iconURL"] = clean_text(icon)

    description = first_non_empty(app.get("localizedDescription"), app.get("note"))
    if not is_empty(description):
        app["localizedDescription"] = clean_text_keep_lines(description)
    else:
        app.pop("localizedDescription", None)

    app["size"] = size_to_bytes(app.get("size"))

    for field in ("addedAt", "updatedAt"):
        if field in app:
            value = normalize_date(app.get(field))
            if value:
                app[field] = value
            else:
                app.pop(field, None)

    version_date = normalize_date(
        first_non_empty(app.get("versionDate"), app.get("updatedAt"), app.get("addedAt"))
    )
    if version_date:
        app["versionDate"] = version_date

    versions = build_versions(app)
    if versions:
        app["versions"] = versions
    else:
        app.pop("versions", None)

    for field in OPTIONAL_FIELDS:
        if field not in raw or is_empty(raw.get(field)):
            app.pop(field, None)
        else:
            app[field] = raw.get(field)

    for field in ("bundleIdentifier", "bundleId", "developerName", "category", "fileType", "version"):
        if field in app and isinstance(app.get(field), str):
            cleaned = clean_text(app.get(field))
            if cleaned:
                app[field] = cleaned
            else:
                app.pop(field, None)

    app.pop("icon", None)
    app.pop("note", None)
    app.pop("previousVersions", None)
    app.pop("hidden", None)

    return order_dict(app, APP_FIELD_ORDER)


def identity(app):
    if not isinstance(app, dict):
        return None
    app_id = clean_text(app.get("id"))
    if app_id:
        return "id:" + app_id.casefold()
    name = clean_text(app.get("name"))
    if name:
        return "name:" + name.casefold()
    url = clean_text(first_non_empty(app.get("downloadURL"), app.get("ipaUrl")))
    if url:
        return "url:" + url.casefold()
    return None


def latest_version_entry(app):
    versions = app.get("versions") if isinstance(app, dict) else None
    if isinstance(versions, list) and versions and isinstance(versions[0], dict):
        return versions[0]
    return {}


def release_signature(app):
    latest = latest_version_entry(app)
    return (
        clean_text(app.get("version")) if isinstance(app, dict) else "",
        clean_text(app.get("downloadURL")) if isinstance(app, dict) else "",
        clean_text(app.get("ipaUrl")) if isinstance(app, dict) else "",
        clean_text(latest.get("version")),
        clean_text(first_non_empty(latest.get("downloadURL"), latest.get("ipaUrl"))),
    )


def version_item_key(item):
    if not isinstance(item, dict):
        return None
    version = clean_text(item.get("version"))
    url = clean_text(first_non_empty(item.get("downloadURL"), item.get("ipaUrl")))
    if not version and not url:
        return None
    return (version, url)


def preserve_version_dates(old_app, new_app, release_changed, today):
    old_versions = old_app.get("versions") if isinstance(old_app, dict) else None
    new_versions = new_app.get("versions") if isinstance(new_app, dict) else None
    if not isinstance(new_versions, list):
        return

    old_dates = {}
    if isinstance(old_versions, list):
        for item in old_versions:
            key = version_item_key(item)
            if key is not None and isinstance(item, dict) and "date" in item:
                old_dates[key] = item.get("date")

    for index, item in enumerate(new_versions):
        if not isinstance(item, dict):
            continue
        key = version_item_key(item)
        if index == 0 and release_changed:
            item["date"] = today
        elif key in old_dates:
            if old_dates[key] is None:
                item.pop("date", None)
            else:
                item["date"] = old_dates[key]


def merge_existing(old_app, incoming, today):
    merged = copy.deepcopy(incoming)
    release_changed = release_signature(old_app) != release_signature(merged)

    for field in DATE_FIELDS:
        if field in old_app:
            merged[field] = old_app.get(field)
        else:
            merged.pop(field, None)

    if release_changed:
        merged["versionDate"] = today
        merged["updatedAt"] = today
        if "addedAt" in old_app:
            merged["addedAt"] = old_app.get("addedAt")

    preserve_version_dates(old_app, merged, release_changed, today)
    return order_dict(merged, APP_FIELD_ORDER), release_changed


def seed_from_source(incoming):
    return order_dict(copy.deepcopy(incoming), APP_FIELD_ORDER)


def build_output(source, target=None, today=None):
    today = today or today_string()
    source_apps = [a for a in source.get("apps", []) if isinstance(a, dict)]
    target_apps = [a for a in (target or {}).get("apps", []) if isinstance(a, dict)]
    first_seed = target is None

    old_by_key = {}
    for app in target_apps:
        key = identity(app)
        if key and key not in old_by_key:
            old_by_key[key] = app

    output_apps = []
    changed_release = []
    metadata_only = []
    new_apps = []

    for raw in source_apps:
        incoming = normalize_app(raw)
        key = identity(incoming)
        old = old_by_key.get(key) if key else None

        if old is None:
            if first_seed:
                merged = seed_from_source(incoming)
            else:
                merged = copy.deepcopy(incoming)
                merged["versionDate"] = today
                merged["addedAt"] = today
                merged["updatedAt"] = today
                versions = merged.get("versions")
                if isinstance(versions, list) and versions and isinstance(versions[0], dict):
                    versions[0]["date"] = today
            output_apps.append(order_dict(merged, APP_FIELD_ORDER))
            new_apps.append(clean_text(merged.get("name") or merged.get("id")))
            continue

        merged, release_changed = merge_existing(old, incoming, today)
        output_apps.append(merged)
        if release_changed:
            changed_release.append(clean_text(merged.get("name") or merged.get("id")))
        elif old != merged:
            metadata_only.append(clean_text(merged.get("name") or merged.get("id")))

    output = {}
    for key, value in source.items():
        if key != "apps":
            output[key] = copy.deepcopy(value)
    output["apps"] = output_apps

    report = {
        "source": len(source_apps),
        "written": len(output_apps),
        "new": new_apps,
        "release_updates": changed_release,
        "metadata_only": metadata_only,
        "first_seed": first_seed,
    }
    return output, report


def validate(output):
    apps = [a for a in output.get("apps", []) if isinstance(a, dict)]
    if not apps:
        raise ValueError("deb-AR.json has no apps")

    seen = set()
    duplicate_ids = []
    empty_optional = []
    bad_sizes = []

    for app in apps:
        app_id = clean_text(app.get("id"))
        if app_id:
            key = app_id.casefold()
            if key in seen:
                duplicate_ids.append(app_id)
            seen.add(key)

        if not isinstance(app.get("size"), int):
            bad_sizes.append(clean_text(app.get("name") or app.get("id")))

        for field in OPTIONAL_FIELDS:
            if field in app and is_empty(app.get(field)):
                empty_optional.append(f"{app.get('name')}: {field}")

    if duplicate_ids:
        raise ValueError(f"duplicate ids: {duplicate_ids[:20]}")
    if bad_sizes:
        raise ValueError(f"size must be int bytes: {bad_sizes[:20]}")
    if empty_optional:
        raise ValueError(f"empty optional fields: {empty_optional[:20]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="source_deb.json")
    parser.add_argument("--target", default="deb-AR.json")
    parser.add_argument("--today", default=None)
    args = parser.parse_args()

    source = load_json(args.source)
    target = load_json(args.target, required=False)
    output, report = build_output(source, target, today=args.today)
    validate(output)
    write_json(args.target, output)

    print(f"✅ source apps: {report['source']}")
    print(f"✅ apps written: {report['written']}")
    print(f"✅ first seed: {report['first_seed']}")
    print(f"✅ new apps: {len(report['new'])}")
    print(f"✅ real release updates: {len(report['release_updates'])}")
    print(f"✅ metadata-only changes (dates preserved): {len(report['metadata_only'])}")
    print("✅ recommended/toolVersion are omitted when empty")
    print("✅ dates move only when version/app URL/release URL changes")


if __name__ == "__main__":
    main()
