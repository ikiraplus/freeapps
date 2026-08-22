#!/usr/bin/env python3
import argparse
import base64
import copy
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import threading
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

START_APP_NAME = "ريكرام"
BUNDLE_PREFIX = "com.ikiraplus.apps"
LOCAL_TIMEZONE = "Asia/Baghdad"


def _unpack(value):
    return base64.b64decode(value[::-1]).decode("utf-8")

SOURCE_FIELD_ORDER = [
    "name",
    "identifier",
    "sourceURL",
    "iconURL",
    "sourceIcon",
    "website",
    "news",
    "apps",
]

APP_FIELD_ORDER = [
    "id",
    "name",
    "bundleIdentifier",
    "bundleId",
    "developerName",
    "iconURL",
    "localizedDescription",
    "category",
    "versions",
    "version",
    "versionDate",
    "downloadURL",
    "ipaUrl",
    "size",
    "addedAt",
    "updatedAt",
    "hidden",
]

TRACKED_UPDATE_FIELDS = [
    "id",
    "name",
    "bundleIdentifier",
    "bundleId",
    "developerName",
    "version",
    "downloadURL",
    "ipaUrl",
    "iconURL",
    "localizedDescription",
    "size",
    "category",
    "versions",
    "previousVersions",
    "hidden",
]

# Fields we never want in the output, no matter where they came from (raw
# source, a previously-synced target file, etc.). Some (toolVersion,
# recommended) are just dropped outright; note/icon are used as one-time
# fallbacks elsewhere (note -> localizedDescription, icon -> iconURL) and then
# dropped so they don't linger as duplicate/junk fields.
FIELDS_TO_DROP = ("toolVersion", "recommended", "note", "icon")


def drop_removed_fields(app):
    if isinstance(app, dict):
        for key in FIELDS_TO_DROP:
            app.pop(key, None)
    return app

DATE_FIELDS = {"versionDate", "addedAt", "updatedAt"}

DEFAULT_SOURCE_META = {
    "name": "iKiraPlus - IPA Store",
    "identifier": "com.ikiraplus.store",
    "sourceURL": _unpack("=UmcvR3chBXav4Wah12LlNmc192cvA3brN2Yhp2Lt92YuQnblRnbvNmclNXdiVHa0l2ZucXYy9yL6MHc0RHa"),
    "iconURL": "https://raw.githubusercontent.com/ikira18/feather/main/images/kiraplus.png",
    "sourceIcon": "https://raw.githubusercontent.com/ikira18/feather/main/images/kiraplus.png",
    "website": "https://t.me/iKiraPlus",
    "news": [
        {
            "title": "كيرا بلس",
            "identifier": "com.ikiraplus.card",
            "caption": "قناة كيرا بلس للتطبيقات والشهادات",
            "date": "2026-05-11",
            "tintColor": "#7A7DFF",
            "imageURL": "https://raw.githubusercontent.com/ikira18/feather/main/images/kiraplus.png",
            "url": "https://t.me/iKiraPlus",
        }
    ],
}

CERTIFICATE_ID_NEEDLES = (
    "shahada-free",
    "shahadda-free",
    "shahada",
    "shahadda",
)
CERTIFICATE_NAME_NEEDLES = (
    "شهاده مجاني",
    "شهاده",
)


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
    """Like clean_text, but preserves line breaks instead of flattening them.

    Used for anything we're about to send to translation, so bullet-style
    descriptions ("- point one\n- point two") keep their line structure in
    the English output instead of collapsing into one run-on sentence.
    """
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"[\u200b\u200c\u200d\ufeff\u2060]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_arabic(value):
    text = clean_text(value)
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي")
    text = text.replace("ة", "ه")
    return text.casefold()


def is_empty(value):
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in {"", "null", "none", "nan"}:
        return True
    return False


def first_non_empty(*values):
    for value in values:
        if not is_empty(value):
            return value
    return None


def is_certificate_app(app):
    if not isinstance(app, dict):
        return False

    name_key = normalize_arabic(app.get("name"))
    id_key = clean_text(app.get("id")).casefold()
    bundle_key = clean_text(app.get("bundleIdentifier") or app.get("bundleId")).casefold()
    url_key = clean_text(app.get("downloadURL") or app.get("ipaUrl")).casefold()
    blob = f"{id_key} {bundle_key} {url_key}"

    if any(needle in name_key for needle in CERTIFICATE_NAME_NEEDLES):
        return True
    if any(needle in blob for needle in CERTIFICATE_ID_NEEDLES):
        return True
    return False


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
    if any(unit in text for unit in ["gb", "gib", "جيجا", "غيغا"]):
        return int(value * 1024 * 1024 * 1024)
    if any(unit in text for unit in ["mb", "mib", "ميجا", "ميكا", "مب"]):
        return int(value * 1024 * 1024)
    if any(unit in text for unit in ["kb", "kib", "كيلو", "كب"]):
        return int(value * 1024)
    return int(value)


def normalize_version_date(value):
    value = first_non_empty(value)
    if value is None:
        return None
    text = str(value).strip()
    if "T" in text:
        text = text.split("T", 1)[0].strip()
    return text or None


def build_versions(app):
    """Build at most 2 AltSource-style versions: current + newest previous."""
    versions = []

    current_version = clean_text(app.get("version"))
    current_url = first_non_empty(app.get("downloadURL"), app.get("ipaUrl"))
    current_date = normalize_version_date(
        first_non_empty(app.get("versionDate"), app.get("updatedAt"), app.get("addedAt"))
    )
    current_size = size_to_bytes(app.get("size"))

    if current_version and not is_empty(current_url):
        current = {
            "version": current_version,
            "downloadURL": clean_text(current_url),
        }
        if current_date:
            current["date"] = current_date
        if current_size > 0:
            current["size"] = current_size
        versions.append(current)

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
            old_date = normalize_version_date(
                first_non_empty(old.get("date"), old.get("versionDate"), old.get("updatedAt"), old.get("addedAt"))
            )
            old_size = size_to_bytes(old.get("size"))
            if old_date:
                item["date"] = old_date
            if old_size > 0:
                item["size"] = old_size
            versions.append(item)

    return versions[:2]


def slugify(text):
    text = clean_text(text).casefold()
    ascii_text = text.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.replace("'", "")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug[:42].strip("-") or "app"


def stable_hash(*parts):
    raw = "|".join(clean_text(part) for part in parts if not is_empty(part))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def make_bundle(app, used_bundles, fallback_bundle=None):
    existing = first_non_empty(app.get("bundleIdentifier"), app.get("bundleId"))
    fallback = clean_text(fallback_bundle)

    if is_empty(existing) and fallback:
        fallback_key = fallback.casefold()
        if fallback_key not in used_bundles:
            used_bundles.add(fallback_key)
            return fallback

    existing_key = clean_text(existing).casefold()
    if existing_key and existing_key not in used_bundles:
        used_bundles.add(existing_key)
        return clean_text(existing)

    base = (
        f"{BUNDLE_PREFIX}."
        f"{slugify(first_non_empty(app.get('name'), app.get('id'), 'app'))}."
        f"{stable_hash(app.get('id'), app.get('name'))}"
    )
    candidate = base
    counter = 2
    while candidate.casefold() in used_bundles:
        candidate = f"{base}.{counter}"
        counter += 1

    used_bundles.add(candidate.casefold())
    return candidate


def order_dict(data, preferred_order):
    ordered = {key: data[key] for key in preferred_order if key in data}
    for key, value in data.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def load_json_file(path, *, required=True):
    if not os.path.exists(path):
        if required:
            raise FileNotFoundError(path)
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    if not isinstance(data.get("apps"), list):
        raise ValueError(f"{path} must contain an apps array")
    return data


def get_apps(source):
    if not isinstance(source, dict):
        return []
    return [app for app in source.get("apps", []) if isinstance(app, dict)]


def find_start_index(apps):
    target = normalize_arabic(START_APP_NAME)
    for index, app in enumerate(apps):
        if normalize_arabic(app.get("name")) == target:
            return index
    names_preview = [clean_text(app.get("name")) for app in apps[:30]]
    raise ValueError(f"Start app '{START_APP_NAME}' was not found. First apps: {names_preview}")


def normalize_app(app, used_bundles, fallback_bundle=None):
    fixed = copy.deepcopy(app)

    if "name" in fixed:
        fixed["name"] = clean_text(fixed.get("name"))

    fixed["size"] = size_to_bytes(fixed.get("size"))
    fixed["versionDate"] = normalize_version_date(
        first_non_empty(fixed.get("versionDate"), fixed.get("updatedAt"), fixed.get("addedAt"))
    )
    if "addedAt" in fixed:
        fixed["addedAt"] = normalize_version_date(fixed.get("addedAt"))
    if "updatedAt" in fixed:
        fixed["updatedAt"] = normalize_version_date(fixed.get("updatedAt"))

    if is_empty(fixed.get("downloadURL")) and not is_empty(fixed.get("ipaUrl")):
        fixed["downloadURL"] = fixed.get("ipaUrl")
    if is_empty(fixed.get("ipaUrl")) and not is_empty(fixed.get("downloadURL")):
        fixed["ipaUrl"] = fixed.get("downloadURL")
    if is_empty(fixed.get("iconURL")) and not is_empty(fixed.get("icon")):
        fixed["iconURL"] = fixed.get("icon")
    if is_empty(fixed.get("icon")) and not is_empty(fixed.get("iconURL")):
        fixed["icon"] = fixed.get("iconURL")
    if is_empty(fixed.get("localizedDescription")) and not is_empty(fixed.get("note")):
        fixed["localizedDescription"] = fixed.get("note")

    fixed["versions"] = build_versions(fixed)
    fixed.pop("previousVersions", None)

    fixed["bundleIdentifier"] = make_bundle(fixed, used_bundles, fallback_bundle=fallback_bundle)
    drop_removed_fields(fixed)
    return order_dict(fixed, APP_FIELD_ORDER)


def strong_identity_keys(app, include_url=True):
    """Return stable identity keys without using the display name.

    Names are intentionally excluded here because the source can contain
    different apps with the same name. We prefer id, bundle id, then URL.
    """
    if not isinstance(app, dict):
        return []

    keys = []
    app_id = first_non_empty(app.get("id"))
    bundle = first_non_empty(app.get("bundleIdentifier"), app.get("bundleId"))
    url = first_non_empty(app.get("downloadURL"), app.get("ipaUrl"))

    if not is_empty(app_id):
        keys.append(f"id:{clean_text(app_id).casefold()}")
    if not is_empty(bundle):
        keys.append(f"bundle:{clean_text(bundle).casefold()}")
    if include_url and not is_empty(url):
        keys.append(f"url:{clean_text(url).casefold()}")

    return list(dict.fromkeys(keys))


def name_identity_key(app):
    if not isinstance(app, dict):
        return None
    name = normalize_arabic(app.get("name"))
    return f"name:{name}" if name else None


def source_record_identity(app):
    """Single dedupe key for source records.

    This prevents true duplicate records while preserving separate apps that
    merely share the same display name.
    """
    keys = strong_identity_keys(app, include_url=True)
    for prefix in ("id:", "bundle:", "url:"):
        for key in keys:
            if key.startswith(prefix):
                return key
    return name_identity_key(app)


def build_target_lookup(target_apps):
    strong_lookup = {}
    duplicate_keys = []
    name_candidates = {}

    for index, app in enumerate(target_apps):
        if is_certificate_app(app):
            continue

        for key in strong_identity_keys(app, include_url=True):
            if key in strong_lookup:
                duplicate_keys.append(key)
            else:
                strong_lookup[key] = index

        name_key = name_identity_key(app)
        if name_key:
            name_candidates.setdefault(name_key, []).append(index)

    # Name matching is only safe when the name occurs exactly once in target.
    unique_name_lookup = {
        key: indexes[0]
        for key, indexes in name_candidates.items()
        if len(indexes) == 1
    }

    return {
        "strong": strong_lookup,
        "unique_names": unique_name_lookup,
    }, duplicate_keys


def find_matching_target_index(raw_app, fixed_app, target_lookup):
    strong_lookup = target_lookup.get("strong", {}) if isinstance(target_lookup, dict) else {}
    unique_name_lookup = target_lookup.get("unique_names", {}) if isinstance(target_lookup, dict) else {}

    keys = []
    keys.extend(strong_identity_keys(raw_app, include_url=True))
    keys.extend(strong_identity_keys(fixed_app, include_url=True))

    for key in dict.fromkeys(keys):
        if key in strong_lookup:
            return strong_lookup[key]

    # Last-resort name match, but only when that name is unique in the target.
    for app in (raw_app, fixed_app):
        name_key = name_identity_key(app)
        if name_key and name_key in unique_name_lookup:
            return unique_name_lookup[name_key]

    return None

def compare_value(key, value):
    if key == "size":
        return size_to_bytes(value)
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return value


def changed_field_names(old_app, new_app):
    changed = []
    for key in TRACKED_UPDATE_FIELDS:
        old_exists = key in old_app and not is_empty(old_app.get(key))
        new_exists = key in new_app and not is_empty(new_app.get(key))
        old_value = compare_value(key, old_app.get(key)) if old_exists else None
        new_value = compare_value(key, new_app.get(key)) if new_exists else None
        if old_exists != new_exists or old_value != new_value:
            changed.append(key)
    return changed


def merge_existing_app(existing_app, incoming_app, today):
    updated = copy.deepcopy(existing_app)
    original_dates = {key: updated.get(key) for key in DATE_FIELDS if key in updated}

    for key in TRACKED_UPDATE_FIELDS:
        if key in updated and key not in incoming_app:
            updated.pop(key, None)

    for key, value in incoming_app.items():
        if key in DATE_FIELDS:
            continue
        updated[key] = value

    changed = changed_field_names(existing_app, updated)

    for key in DATE_FIELDS:
        if key in updated:
            updated.pop(key, None)
        if key in original_dates:
            updated[key] = original_dates[key]

    if changed:
        updated["versionDate"] = today
        updated["updatedAt"] = today
        if is_empty(updated.get("addedAt")):
            updated["addedAt"] = today
    else:
        if is_empty(updated.get("versionDate")):
            updated["versionDate"] = normalize_version_date(incoming_app.get("versionDate")) or today
        if is_empty(updated.get("updatedAt")) and not is_empty(incoming_app.get("updatedAt")):
            updated["updatedAt"] = normalize_version_date(incoming_app.get("updatedAt"))
        if is_empty(updated.get("addedAt")) and not is_empty(incoming_app.get("addedAt")):
            updated["addedAt"] = normalize_version_date(incoming_app.get("addedAt"))

    updated["versionDate"] = normalize_version_date(updated.get("versionDate")) or today
    if not is_empty(updated.get("addedAt")):
        updated["addedAt"] = normalize_version_date(updated.get("addedAt"))
    if not is_empty(updated.get("updatedAt")):
        updated["updatedAt"] = normalize_version_date(updated.get("updatedAt"))

    return order_dict(updated, APP_FIELD_ORDER), changed


def new_app_with_dates(incoming_app, today):
    fixed = copy.deepcopy(incoming_app)
    fixed["versionDate"] = today
    fixed["addedAt"] = today
    fixed["updatedAt"] = today
    return order_dict(fixed, APP_FIELD_ORDER)


def prepare_source_records(jom_source, target_apps):
    jom_apps = get_apps(jom_source)
    start_index = find_start_index(jom_apps)
    ignored_before_start = jom_apps[:start_index]
    target_lookup, duplicate_target_keys = build_target_lookup(target_apps)

    used_bundles = set()
    seen_source_keys = set()
    source_records = []
    skipped_certificates = []
    skipped_source_duplicates = []
    target_match_collisions = []

    for source_index, raw_app in enumerate(jom_apps[start_index:], start=start_index):
        if is_certificate_app(raw_app):
            skipped_certificates.append(clean_text(raw_app.get("name") or raw_app.get("id")))
            continue

        preliminary_match = find_matching_target_index(raw_app, {}, target_lookup)
        fallback_bundle = None
        if preliminary_match is not None and preliminary_match < len(target_apps):
            fallback_bundle = first_non_empty(
                target_apps[preliminary_match].get("bundleIdentifier"),
                target_apps[preliminary_match].get("bundleId"),
            )

        fixed_app = normalize_app(raw_app, used_bundles, fallback_bundle=fallback_bundle)
        if is_certificate_app(fixed_app):
            skipped_certificates.append(clean_text(fixed_app.get("name") or fixed_app.get("id")))
            continue

        match_index = find_matching_target_index(raw_app, fixed_app, target_lookup)
        record_key = source_record_identity(raw_app) or source_record_identity(fixed_app)

        if record_key and record_key in seen_source_keys:
            skipped_source_duplicates.append(clean_text(fixed_app.get("name") or fixed_app.get("id")))
            continue
        if record_key:
            seen_source_keys.add(record_key)

        source_records.append(
            {
                "source_index": source_index,
                "raw": raw_app,
                "fixed": fixed_app,
                "match_index": match_index,
                "keys": [record_key] if record_key else [],
            }
        )

    matched_by_target_index = {}
    unique_source_records = []
    for record in source_records:
        match_index = record["match_index"]
        if match_index is not None:
            if match_index in matched_by_target_index:
                target_match_collisions.append(clean_text(record["fixed"].get("name") or record["fixed"].get("id")))
                continue
            matched_by_target_index[match_index] = record
        unique_source_records.append(record)

    report = {
        "ignored_before_start": ignored_before_start,
        "skipped_certificates": skipped_certificates,
        "skipped_source_duplicates": skipped_source_duplicates,
        "duplicate_target_keys": duplicate_target_keys,
        "target_match_collisions": target_match_collisions,
    }
    return unique_source_records, matched_by_target_index, report


def build_source_metadata(jom_source, target_source=None):
    # Preserve the destination source metadata (name/icon/sourceURL/news/etc.)
    # and only synchronize the apps array. Missing destination metadata falls
    # back to jom.json, then to DEFAULT_SOURCE_META.
    clean_source = {}

    if isinstance(target_source, dict):
        for key, value in target_source.items():
            if key != "apps":
                clean_source[key] = copy.deepcopy(value)

    for key, value in jom_source.items():
        if key != "apps" and key not in clean_source:
            clean_source[key] = copy.deepcopy(value)

    for key, value in DEFAULT_SOURCE_META.items():
        if key not in clean_source:
            clean_source[key] = copy.deepcopy(value)

    clean_source.setdefault("name", DEFAULT_SOURCE_META["name"])
    clean_source.setdefault("identifier", DEFAULT_SOURCE_META["identifier"])
    clean_source.setdefault("sourceURL", DEFAULT_SOURCE_META["sourceURL"])
    clean_source.setdefault("iconURL", DEFAULT_SOURCE_META["iconURL"])
    clean_source.setdefault("sourceIcon", clean_source.get("iconURL", DEFAULT_SOURCE_META["sourceIcon"]))
    clean_source.setdefault("website", DEFAULT_SOURCE_META["website"])
    if not isinstance(clean_source.get("news"), list) or not clean_source.get("news"):
        clean_source["news"] = copy.deepcopy(DEFAULT_SOURCE_META["news"])

    return clean_source


def build_source_from_regram(jom_source, target_source=None, today=None):
    today = today or today_string()
    target_apps = get_apps(target_source)
    source_records, matched_by_target_index, report = prepare_source_records(jom_source, target_apps)

    output_apps = []
    used_record_ids = set()
    changed_apps = []
    unchanged_apps = []
    new_apps = []
    removed_apps = []

    if target_apps:
        for target_index, existing_app in enumerate(target_apps):
            if is_certificate_app(existing_app):
                removed_apps.append(clean_text(existing_app.get("name") or existing_app.get("id")))
                continue

            record = matched_by_target_index.get(target_index)
            if record is None:
                removed_apps.append(clean_text(existing_app.get("name") or existing_app.get("id")))
                continue

            merged_app, changed_fields = merge_existing_app(existing_app, record["fixed"], today)
            output_apps.append(merged_app)
            used_record_ids.add(id(record))

            if changed_fields:
                changed_apps.append(
                    {
                        "name": clean_text(merged_app.get("name") or merged_app.get("id")),
                        "fields": changed_fields,
                    }
                )
            else:
                unchanged_apps.append(clean_text(merged_app.get("name") or merged_app.get("id")))

    for record in source_records:
        if id(record) in used_record_ids:
            continue
        app = new_app_with_dates(record["fixed"], today)
        output_apps.append(app)
        new_apps.append(clean_text(app.get("name") or app.get("id")))

    for app in output_apps:
        drop_removed_fields(app)

    clean_source = build_source_metadata(jom_source, target_source)
    clean_source["apps"] = output_apps

    report.update(
        {
            "today": today,
            "changed_apps": changed_apps,
            "unchanged_apps": unchanged_apps,
            "new_apps": new_apps,
            "removed_apps": removed_apps,
            "source_records": source_records,
        }
    )
    return order_dict(clean_source, SOURCE_FIELD_ORDER), report


def validate_output(output_source, source_records, ignored_before_start):
    output_apps = [app for app in output_source.get("apps", []) if isinstance(app, dict)]
    expected_names = [clean_text(record["fixed"].get("name")) for record in source_records]
    output_names = [clean_text(app.get("name")) for app in output_apps]

    if not output_apps:
        raise ValueError("Output has no apps")

    first_name = output_apps[0].get("name")
    if normalize_arabic(first_name) != normalize_arabic(START_APP_NAME):
        raise ValueError(f"First app must be '{START_APP_NAME}', got '{first_name}'")

    if Counter(output_names) != Counter(expected_names):
        missing = list((Counter(expected_names) - Counter(output_names)).elements())[:20]
        extra = list((Counter(output_names) - Counter(expected_names)).elements())[:20]
        raise ValueError(f"Output app set does not match source apps. Missing: {missing}. Extra: {extra}")

    certificate_apps = [clean_text(app.get("name") or app.get("id")) for app in output_apps if is_certificate_app(app)]
    if certificate_apps:
        raise ValueError(f"Certificate apps leaked into output: {certificate_apps[:20]}")

    ignored_names = {clean_text(app.get("name")) for app in ignored_before_start if clean_text(app.get("name"))}
    leaked_before_start = [name for name in output_names if name in ignored_names]
    if leaked_before_start:
        raise ValueError(f"Apps before ريكرام leaked into output: {leaked_before_start[:20]}")

    empty_dates = [app.get("name") for app in output_apps if is_empty(app.get("versionDate"))]
    if empty_dates:
        raise ValueError(f"Empty versionDate values found: {empty_dates[:20]}")

    bad_sizes = [app.get("name") for app in output_apps if not isinstance(app.get("size"), int)]
    if bad_sizes:
        raise ValueError(f"Size must be int bytes for: {bad_sizes[:20]}")

    missing_bundles = [app.get("name") for app in output_apps if is_empty(app.get("bundleIdentifier"))]
    if missing_bundles:
        raise ValueError(f"Missing bundleIdentifier values: {missing_bundles[:20]}")

    bundle_counts = Counter(clean_text(app.get("bundleIdentifier")).casefold() for app in output_apps if app.get("bundleIdentifier"))
    duplicate_bundles = [bundle for bundle, count in bundle_counts.items() if count > 1]
    if duplicate_bundles:
        raise ValueError(f"Duplicate bundleIdentifier values: {duplicate_bundles[:20]}")

    id_counts = Counter(clean_text(app.get("id")).casefold() for app in output_apps if not is_empty(app.get("id")))
    duplicate_ids = [app_id for app_id, count in id_counts.items() if count > 1]
    if duplicate_ids:
        raise ValueError(f"Duplicate id values: {duplicate_ids[:20]}")


    bad_versions = []
    for app in output_apps:
        versions = app.get("versions")
        name = clean_text(app.get("name") or app.get("id"))
        if not isinstance(versions, list) or not versions or len(versions) > 2:
            bad_versions.append(f"{name}: invalid versions count")
            continue
        if clean_text(versions[0].get("version")) != clean_text(app.get("version")):
            bad_versions.append(f"{name}: latest version mismatch")
            continue
        current_url = clean_text(first_non_empty(app.get("downloadURL"), app.get("ipaUrl")))
        if clean_text(versions[0].get("downloadURL")) != current_url:
            bad_versions.append(f"{name}: latest downloadURL mismatch")
            continue
        seen_versions = set()
        for item in versions:
            if not isinstance(item, dict) or is_empty(item.get("version")) or is_empty(item.get("downloadURL")):
                bad_versions.append(f"{name}: incomplete version entry")
                break
            version_key = clean_text(item.get("version")).casefold()
            if version_key in seen_versions:
                bad_versions.append(f"{name}: duplicate version {item.get('version')}")
                break
            seen_versions.add(version_key)

    if bad_versions:
        raise ValueError(f"Invalid versions data: {bad_versions[:20]}")


def hard_write_json(path, data):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    try:
        if os.path.exists(path):
            os.chmod(path, 0o666)
    except FileNotFoundError:
        pass

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    os.replace(tmp_path, path)


def print_report(path, report, apps_count):
    print("✅ SMART SYNC OK")
    print(f"✅ target synced: {path}")
    print(f"✅ today date used for changed/new apps: {report['today']}")
    print(f"✅ ignored before ريكرام: {len(report['ignored_before_start'])}")
    for app in report["ignored_before_start"][:20]:
        print(f"   - ignored: {clean_text(app.get('name') or app.get('id'))}")

    print(f"✅ changed apps refreshed to today: {len(report['changed_apps'])}")
    for item in report["changed_apps"][:30]:
        print(f"   - updated: {item['name']} | fields: {', '.join(item['fields'])}")

    print(f"✅ new apps added: {len(report['new_apps'])}")
    for name in report["new_apps"][:30]:
        print(f"   - new: {name}")

    print(f"✅ removed apps not in source / duplicates / certificates: {len(report['removed_apps'])}")
    for name in report["removed_apps"][:30]:
        print(f"   - removed: {name}")

    print(f"✅ unchanged apps kept with same dates: {len(report['unchanged_apps'])}")
    print(f"✅ skipped certificate apps after ريكرام: {len(report['skipped_certificates'])}")
    for name in report["skipped_certificates"][:20]:
        print(f"   - skipped certificate: {name}")

    print(f"✅ skipped duplicate source apps: {len(report['skipped_source_duplicates'])}")
    for name in report["skipped_source_duplicates"][:20]:
        print(f"   - skipped duplicate: {name}")

    if report["duplicate_target_keys"]:
        print(f"⚠️ duplicate keys found in old target and safely collapsed: {len(report['duplicate_target_keys'])}")
    if report["target_match_collisions"]:
        print(f"⚠️ target match collisions skipped: {len(report['target_match_collisions'])}")

    print(f"✅ apps written: {apps_count}")
    print("✅ existing apps keep their same order")
    print("✅ changed app info updates versionDate/updatedAt only, no second copy")
    print("✅ no شهادة مجانية apps in output")
    print("✅ size is bytes/int")
    print("✅ versionDate is never empty")
    print("✅ bundleIdentifier values are unique")



# Free, unofficial Google Translate endpoint (the same one translate.google.com's
# web page itself calls). No account, no API key, no billing. It's not an
# officially documented/supported API, so we're deliberately conservative about
# request rate to avoid tripping any abuse protection — see the rate limiter below.
GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"

# Undocumented endpoint => no published quota to target. We pick a modest, safe
# rate on purpose (tunable via env) rather than hammering it.
TRANSLATE_MAX_REQUESTS_PER_MINUTE = int(os.getenv("TRANSLATE_MAX_RPM", "20"))


class _SlidingWindowRateLimiter:
    """Blocks callers so that at most `max_calls` happen in any rolling `period` window.

    Shared across all translation threads so the *combined* request rate never
    exceeds our self-imposed cap, no matter how many workers run concurrently.
    """

    def __init__(self, max_calls, period=60.0):
        self.max_calls = max_calls
        self.period = period
        self._lock = threading.Lock()
        self._timestamps = deque()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self.period:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.max_calls:
                    self._timestamps.append(now)
                    return

                wait_time = self.period - (now - self._timestamps[0])

            if wait_time > 0:
                time.sleep(wait_time)


_translate_rate_limiter = _SlidingWindowRateLimiter(TRANSLATE_MAX_REQUESTS_PER_MINUTE)


# ---------------------------------------------------------------------------
# Glossary: domain terms that Google Translate frequently mistranslates
# because it has no idea this is app-store / certificate / iOS-signing text.
# Each Arabic term below is swapped for the given English term BEFORE the
# text is sent to Google, so it never gets machine-guessed at all — it comes
# back exactly as written here.
#
# To add or fix a term: just add/edit a line "arabic": "english" below.
# No other code needs to change. Longer phrases are matched before shorter
# ones automatically, so e.g. "كسر الحماية" won't get broken up by "كسر".
# ---------------------------------------------------------------------------
GLOSSARY = {
    "كسر الحماية": "jailbreak",
    "بدون جلبريك": "no jailbreak needed",
    "شهادة مطور": "developer certificate",
    "شهاده مطور": "developer certificate",
    "شهادة مجانية": "free certificate",
    "شهاده مجانيه": "free certificate",
    "شهادة": "certificate",
    "شهاده": "certificate",
    "اعادة توقيع": "re-signing",
    "إعادة توقيع": "re-signing",
    "توقيع التطبيق": "app signing",
    "توقيع": "signing",
    "تفعيل": "activation",
    "تحديث تلقائي": "auto-update",
    "تحديث": "update",
    "نسخة معدلة": "modded version",
    "نسخه معدله": "modded version",
    "نسخة مهكرة": "hacked version",
    "نسخه مهكره": "hacked version",
    "نسخة": "version",
    "نسخه": "version",
    "مهكرة": "hacked",
    "مهكره": "hacked",
    "معدل": "modded",
    "مصدر خارجي": "third-party source",
    "متجر تطبيقات": "app store",
    "متجر": "store",
    "شراء داخل التطبيق": "in-app purchases",
    "مشتريات داخلية": "in-app purchases",
    "بدون اعلانات": "ad-free",
    "بدون إعلانات": "ad-free",
    "اعلانات": "ads",
    "إعلانات": "ads",
    "قناة تليجرام": "Telegram channel",
    "قناة": "channel",
    "جهاز": "device",
    "اصدار": "version",
    "إصدار": "version",
    "مجاني": "free",
    "مجانا": "for free",
    "مدفوع": "paid",
    "متوافق مع": "compatible with",
    "لا يعمل": "not working",
    "يعمل": "working",
    "دعم فني": "support",
    "اشتراك": "subscription",
    "رابط التحميل": "download link",
    "رابط": "link",
    "تحميل": "download",
    "تثبيت": "installation",
    "تحويل لغة": "change the language",
}

_GLOSSARY_SORTED = sorted(GLOSSARY.items(), key=lambda kv: len(kv[0]), reverse=True)


def _protect_glossary_terms(text):
    """Swap known Arabic terms for placeholders before translation.

    Google leaves opaque all-caps tokens alone (same reason URLs and numbers
    survive translation untouched), so whatever we put in tokens comes back
    unchanged and gets swapped for our chosen English wording afterward.
    """
    tokens = {}
    protected = text
    for index, (arabic_term, english_term) in enumerate(_GLOSSARY_SORTED):
        if arabic_term and arabic_term in protected:
            placeholder = f"QQTERM{index}QQ"
            protected = protected.replace(arabic_term, f" {placeholder} ")
            tokens[placeholder] = english_term
    return protected, tokens


def _restore_glossary_terms(text, tokens):
    for placeholder, english_term in tokens.items():
        # Be lenient about stray spaces Google sometimes inserts inside
        # all-caps tokens (e.g. "QQ TERM0QQ").
        loose_pattern = r"\s*".join(re.escape(ch) for ch in placeholder)
        text = re.sub(loose_pattern, f" {english_term} ", text, flags=re.IGNORECASE)
    return re.sub(r" {2,}", " ", text).strip()


def _parse_google_translate_response(raw_body):
    """Parse the nested-array JSON that translate_a/single returns.

    Google splits long text into multiple sentence chunks; each chunk's
    translation is the first element of its own small array. We join them.
    """
    data = json.loads(raw_body)
    segments = data[0] if data else None
    if not segments:
        return ""
    pieces = [seg[0] for seg in segments if seg and seg[0]]
    return "".join(pieces)


def _translate_line_to_english(text):
    """Translate a single line/chunk of Arabic text via Google's free web-translate endpoint."""
    text = clean_text(text)
    if not text:
        return ""

    protected_text, glossary_tokens = _protect_glossary_terms(text)

    params = urllib.parse.urlencode(
        {
            "client": "gtx",
            "sl": "ar",
            "tl": "en",
            "dt": "t",
            "q": protected_text,
        }
    )
    request = urllib.request.Request(
        f"{GOOGLE_TRANSLATE_URL}?{params}",
        headers={
            # A normal browser User-Agent avoids being treated as an obvious bot.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        },
        method="GET",
    )

    last_error = None
    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        # Wait for a free slot in our own self-imposed quota window before every
        # attempt, including retries, so transient blocks can't turn into a
        # hammering loop.
        _translate_rate_limiter.acquire()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw_body = response.read().decode("utf-8")
            result = clean_text(_parse_google_translate_response(raw_body))
            if not result:
                raise RuntimeError("Google Translate returned an empty translation")
            return _restore_glossary_terms(result, glossary_tokens)
        except urllib.error.HTTPError as exc:
            last_error = RuntimeError(f"Google Translate HTTP {exc.code}")
            if exc.code in {429, 403}:
                # Likely a soft/temporary throttle. Back off generously.
                delay = min(10 * attempt, 60)
                print(f"⏳ Google Translate throttled, waiting {delay}s before retry ({attempt}/{max_attempts})")
                time.sleep(delay)
                continue
            if exc.code not in {408, 500, 502, 503, 504}:
                raise last_error
        except (urllib.error.URLError, TimeoutError, IndexError, json.JSONDecodeError) as exc:
            last_error = RuntimeError(f"Google Translate request failed: {exc}")

        if attempt < max_attempts:
            time.sleep(attempt * 2)

    raise last_error or RuntimeError("Google Translate failed")


def translate_to_english(text):
    """Translate Arabic text to English, preserving line breaks.

    Google's translate_a/single endpoint tends to flatten multi-line input
    (bullet lists, numbered steps) into one run-on sentence. To keep that
    structure ("- point one" / "- point two") intact in English, each line
    is translated on its own and the result is rejoined with the same line
    breaks. Blank lines are preserved without spending a request on them.
    """
    text = clean_text_keep_lines(text)
    if not text:
        return ""

    lines = text.split("\n")
    translated_lines = [
        _translate_line_to_english(line) if line.strip() else ""
        for line in lines
    ]
    return "\n".join(translated_lines).strip()


def app_identity_for_translation(app):
    """Stable identity used to match old AR/EN records across sync runs."""
    keys = strong_identity_keys(app, include_url=True)
    for prefix in ("id:", "bundle:", "url:"):
        for key in keys:
            if key.startswith(prefix):
                return key
    return name_identity_key(app)


TRANSLATE_WORKERS = int(os.getenv("TRANSLATE_WORKERS", "3"))

# The EN file is hosted at its own URL and must self-report that, not IPA-AR.json's URL.
EN_SOURCE_URL = "https://ikiraplus.pages.dev/IPA-EN.json"


# Bump this whenever the translation logic changes in a way that makes
# previously-saved English text stale (e.g. line-break handling, glossary
# terms, category/caption translation being added). On the next run, any
# existing IPA-EN.json stamped with an older version is treated as "nothing
# to reuse" so everything gets retranslated once with the current logic —
# after that, normal reuse-if-unchanged caching resumes as usual.
TRANSLATION_FORMAT_VERSION = 2


def build_english_source(ar_source, old_ar_source=None, old_en_source=None):
    """Create IPA-EN from IPA-AR, translating only descriptions that need work.

    All app metadata is copied from IPA-AR, except sourceURL, which is overridden
    to point at the English file's own hosted location. Existing English
    descriptions are kept when the corresponding Arabic description did not
    change; this avoids re-translating on every scheduled run. Apps that do need
    translation are sent to Google's free translate endpoint concurrently (see
    TRANSLATE_WORKERS) instead of one at a time, since each request is a slow,
    mostly-idle network call.
    """
    if not isinstance(old_en_source, dict) or old_en_source.get("translationFormatVersion") != TRANSLATION_FORMAT_VERSION:
        if isinstance(old_en_source, dict) and old_en_source.get("apps"):
            print("♻️ Translation logic changed since the last run — retranslating everything once.")
        old_en_source = None

    old_ar_apps = get_apps(old_ar_source)
    old_en_apps = get_apps(old_en_source)

    old_ar_by_key = {}
    old_en_by_key = {}
    for app in old_ar_apps:
        key = app_identity_for_translation(app)
        if key:
            old_ar_by_key[key] = app
    for app in old_en_apps:
        key = app_identity_for_translation(app)
        if key:
            old_en_by_key[key] = app

    output = copy.deepcopy(ar_source)
    output["sourceURL"] = EN_SOURCE_URL
    translated = 0
    reused = 0
    empty = 0
    pending = []  # (app, arabic_description) pairs that need a fresh translation

    for app in output.get("apps", []):
        if not isinstance(app, dict):
            continue

        key = app_identity_for_translation(app)
        old_ar = old_ar_by_key.get(key) if key else None
        old_en = old_en_by_key.get(key) if key else None

        arabic_description = clean_text_keep_lines(app.get("localizedDescription"))
        old_ar_description = clean_text_keep_lines(old_ar.get("localizedDescription")) if old_ar else ""
        existing_english = clean_text_keep_lines(old_en.get("localizedDescription")) if old_en else ""

        # No Arabic description: don't invent one.
        if not arabic_description:
            app["localizedDescription"] = ""
            empty += 1
            continue

        # Reuse the previous English translation if the Arabic source text is unchanged.
        if existing_english and old_ar_description == arabic_description:
            app["localizedDescription"] = existing_english
            reused += 1
            continue

        pending.append((app, arabic_description))

    if pending:
        print(f"🌐 Translating {len(pending)} app(s) using up to {TRANSLATE_WORKERS} workers")
        with ThreadPoolExecutor(max_workers=min(TRANSLATE_WORKERS, len(pending))) as executor:
            future_to_app = {
                executor.submit(translate_to_english, arabic_description): app
                for app, arabic_description in pending
            }
            for future in as_completed(future_to_app):
                app = future_to_app[future]
                app_label = clean_text(app.get("name") or app.get("id"))
                app["localizedDescription"] = future.result()
                translated += 1
                print(f"🌐 Translated ({translated}/{len(pending)}): {app_label}")

    print(f"🌐 English descriptions translated: {translated}")
    print(f"♻️ English descriptions reused: {reused}")
    print(f"ℹ️ Apps without Arabic descriptions: {empty}")

    translate_categories(output, old_ar_source=old_ar_source, old_en_source=old_en_source)
    translate_news_captions(output, old_ar_source=old_ar_source, old_en_source=old_en_source)

    output["translationFormatVersion"] = TRANSLATION_FORMAT_VERSION

    return output


def translate_categories(output, old_ar_source=None, old_en_source=None):
    """Translate each app's category (e.g. 'العاب' -> 'Games') for the EN file.

    Each distinct Arabic category value is translated once and reused across
    every app that shares it, and reused across runs when the app's category
    text hasn't changed, instead of re-translating per app every time.
    """
    old_ar_apps = get_apps(old_ar_source)
    old_en_apps = get_apps(old_en_source)
    old_ar_by_key = {}
    old_en_by_key = {}
    for app in old_ar_apps:
        key = app_identity_for_translation(app)
        if key:
            old_ar_by_key[key] = app
    for app in old_en_apps:
        key = app_identity_for_translation(app)
        if key:
            old_en_by_key[key] = app

    category_map = {}
    pending_categories = set()

    for app in output.get("apps", []):
        if not isinstance(app, dict):
            continue
        category = clean_text(app.get("category"))
        if not category:
            continue

        key = app_identity_for_translation(app)
        old_ar = old_ar_by_key.get(key) if key else None
        old_en = old_en_by_key.get(key) if key else None
        old_ar_category = clean_text(old_ar.get("category")) if old_ar else ""
        existing_english_category = clean_text(old_en.get("category")) if old_en else ""

        if existing_english_category and old_ar_category == category:
            category_map.setdefault(category, existing_english_category)
        else:
            pending_categories.add(category)

    # A category already resolved via reuse for one app is known for all apps
    # sharing that same Arabic text, so don't re-translate it.
    pending_categories -= set(category_map.keys())

    if pending_categories:
        with ThreadPoolExecutor(max_workers=min(TRANSLATE_WORKERS, len(pending_categories))) as executor:
            future_to_category = {
                executor.submit(translate_to_english, category): category
                for category in pending_categories
            }
            for future in as_completed(future_to_category):
                category = future_to_category[future]
                category_map[category] = future.result()
        print(f"🌐 Categories translated: {len(pending_categories)}")

    for app in output.get("apps", []):
        if not isinstance(app, dict):
            continue
        category = clean_text(app.get("category"))
        if category and category in category_map:
            app["category"] = category_map[category]


def translate_news_captions(output, old_ar_source=None, old_en_source=None):
    """Translate news[].caption (e.g. the channel blurb) for the EN file.

    Titles are left untouched since they're brand names ("كيرا بلس"), not
    descriptive text.
    """
    news_list = output.get("news")
    if not isinstance(news_list, list):
        return

    old_ar_news = old_ar_source.get("news") if isinstance(old_ar_source, dict) else None
    old_en_news = old_en_source.get("news") if isinstance(old_en_source, dict) else None

    def by_identifier(items):
        result = {}
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and not is_empty(item.get("identifier")):
                    result[clean_text(item.get("identifier"))] = item
        return result

    old_ar_by_id = by_identifier(old_ar_news)
    old_en_by_id = by_identifier(old_en_news)

    for item in news_list:
        if not isinstance(item, dict):
            continue
        caption = clean_text_keep_lines(item.get("caption"))
        if not caption:
            continue

        identifier = clean_text(item.get("identifier"))
        old_ar_item = old_ar_by_id.get(identifier)
        old_en_item = old_en_by_id.get(identifier)
        old_ar_caption = clean_text_keep_lines(old_ar_item.get("caption")) if old_ar_item else ""
        existing_english_caption = clean_text_keep_lines(old_en_item.get("caption")) if old_en_item else ""

        if existing_english_caption and old_ar_caption == caption:
            item["caption"] = existing_english_caption
        else:
            item["caption"] = translate_to_english(caption)



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="jom.json", help="Path to jom.json")
    parser.add_argument("--target", default="target_repo/IPA-AR.json", help="Path to target IPA-AR.json source file")
    parser.add_argument("--target-en", default=None, help="Optional path to IPA-EN.json; generated from the Arabic output")
    parser.add_argument("--today", default=None, help="Override today's date as YYYY-MM-DD, useful for tests")
    parser.add_argument(
        "--translate-only",
        action="store_true",
        help=(
            "Skip the source sync entirely and only (re)generate IPA-EN.json from an "
            "already-existing IPA-AR.json (given via --target). Used to run AR->EN "
            "translation as its own independent job, decoupled from the AR sync."
        ),
    )
    parser.add_argument(
        "--old-target",
        default=None,
        help=(
            "Translate-only mode: path to the previous version of IPA-AR.json (e.g. from "
            "git history), used to detect which descriptions actually changed so unchanged "
            "ones can reuse their existing English translation instead of re-translating."
        ),
    )
    args = parser.parse_args()

    if args.translate_only:
        if not args.target_en:
            raise SystemExit("--translate-only requires --target-en")

        ar_source = load_json_file(args.target)
        old_ar_source = load_json_file(args.old_target, required=False) if args.old_target else None
        old_en_source = load_json_file(args.target_en, required=False)

        english_source = build_english_source(
            ar_source,
            old_ar_source=old_ar_source,
            old_en_source=old_en_source,
        )
        hard_write_json(args.target_en, english_source)

        english_written = load_json_file(args.target_en)
        if len(get_apps(english_written)) != len(get_apps(ar_source)):
            raise ValueError("IPA-EN.json app count does not match IPA-AR.json")
        print(f"✅ target synced: {args.target_en}")
        print(f"✅ apps written: {len(get_apps(english_written))}")
        return

    jom_source = load_json_file(args.source)
    target_source = load_json_file(args.target, required=False)
    old_en_source = load_json_file(args.target_en, required=False) if args.target_en else None

    output_source, report = build_source_from_regram(jom_source, target_source, today=args.today)
    validate_output(output_source, report["source_records"], report["ignored_before_start"])
    hard_write_json(args.target, output_source)

    written = load_json_file(args.target)
    validate_output(written, report["source_records"], report["ignored_before_start"])

    if args.target_en:
        english_source = build_english_source(
            written,
            old_ar_source=target_source,
            old_en_source=old_en_source,
        )
        hard_write_json(args.target_en, english_source)
        english_written = load_json_file(args.target_en)
        if len(get_apps(english_written)) != len(get_apps(written)):
            raise ValueError("IPA-EN.json app count does not match IPA-AR.json")
        print(f"✅ target synced: {args.target_en}")

    apps = written["apps"]
    print_report(args.target, report, len(apps))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"❌ sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
