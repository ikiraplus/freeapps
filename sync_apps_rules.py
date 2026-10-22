#!/usr/bin/env python3
"""Runtime rules layered on top of sync_apps.py.

Keeps the existing sync/translation engine intact while enforcing two public-feed rules:
1) recommended/toolVersion are copied only when the source actually provides a non-empty value.
2) existing app dates move only when the current release identity changes (version or IPA/release URL).
"""

import copy
import sys

import sync_apps as core


OPTIONAL_SOURCE_FIELDS = ("recommended", "toolVersion")
RELEASE_DATE_FIELDS = ("versionDate", "updatedAt")


# Keep the optional source metadata in a stable, readable position in AR/EN.
core.APP_FIELD_ORDER = [
    "id",
    "name",
    "bundleIdentifier",
    "bundleId",
    "developerName",
    "iconURL",
    "localizedDescription",
    "category",
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

# These fields still participate in normal data syncing/removal, but NOT in
# the decision to refresh an app's dates.
core.TRACKED_UPDATE_FIELDS = list(
    dict.fromkeys(core.TRACKED_UPDATE_FIELDS + ["recommended", "toolVersion"])
)

# recommended/toolVersion used to be stripped here. Keep them now; only the
# internal/source-only fields remain removable.
core.FIELDS_TO_DROP = ("note", "icon", "hidden")


def _optional_value_is_missing(value):
    return core.is_empty(value)


def drop_removed_fields(app):
    """Drop internal fields and omit optional metadata when source has no value."""
    if not isinstance(app, dict):
        return app

    for key in core.FIELDS_TO_DROP:
        app.pop(key, None)

    for key in OPTIONAL_SOURCE_FIELDS:
        if key not in app or _optional_value_is_missing(app.get(key)):
            app.pop(key, None)

    return app


# normalize_app() and the final output cleanup resolve this global at runtime,
# so replacing it here changes both AR generation and the AR data later copied
# into EN without touching the translation engine itself.
core.drop_removed_fields = drop_removed_fields


def _clean_release_text(value):
    if core.is_empty(value):
        return ""
    return core.clean_text(value)


def _latest_version_entry(app):
    versions = app.get("versions") if isinstance(app, dict) else None
    if isinstance(versions, list) and versions and isinstance(versions[0], dict):
        return versions[0]
    return {}


def release_signature(app):
    """Fields that are allowed to move versionDate/updatedAt.

    We check both top-level IPA URLs and the latest versions[] entry so a
    release-link change is detected even if one representation changes first.
    """
    latest = _latest_version_entry(app)
    return (
        _clean_release_text(app.get("version") if isinstance(app, dict) else None),
        _clean_release_text(app.get("downloadURL") if isinstance(app, dict) else None),
        _clean_release_text(app.get("ipaUrl") if isinstance(app, dict) else None),
        _clean_release_text(latest.get("version")),
        _clean_release_text(latest.get("downloadURL")),
    )


def _version_item_key(item):
    if not isinstance(item, dict):
        return None
    version = _clean_release_text(item.get("version"))
    url = _clean_release_text(item.get("downloadURL") or item.get("ipaUrl"))
    if not version and not url:
        return None
    return (version, url)


def _preserve_version_item_dates(existing_app, updated_app, release_changed, today):
    """Avoid incidental versions[].date churn from metadata-only edits."""
    old_versions = existing_app.get("versions") if isinstance(existing_app, dict) else None
    new_versions = updated_app.get("versions") if isinstance(updated_app, dict) else None
    if not isinstance(new_versions, list):
        return

    old_date_by_key = {}
    if isinstance(old_versions, list):
        for item in old_versions:
            key = _version_item_key(item)
            if key is not None and isinstance(item, dict) and "date" in item:
                old_date_by_key[key] = item.get("date")

    for index, item in enumerate(new_versions):
        if not isinstance(item, dict):
            continue
        key = _version_item_key(item)

        # Current release date moves only with a real release change.
        if index == 0 and release_changed:
            item["date"] = today
            continue

        # Otherwise keep the exact previous date for the same version/link.
        if key in old_date_by_key:
            if old_date_by_key[key] is None:
                item.pop("date", None)
            else:
                item["date"] = old_date_by_key[key]


def merge_existing_app(existing_app, incoming_app, today):
    """Merge all metadata, but refresh dates only for a real release change."""
    updated = copy.deepcopy(existing_app)
    original_dates = {key: updated.get(key) for key in core.DATE_FIELDS if key in updated}

    # Remove tracked fields that disappeared from the source (including the
    # two optional metadata fields), then apply all current source data.
    for key in core.TRACKED_UPDATE_FIELDS:
        if key in updated and key not in incoming_app:
            updated.pop(key, None)

    for key, value in incoming_app.items():
        if key in core.DATE_FIELDS:
            continue
        updated[key] = copy.deepcopy(value)

    changed = core.changed_field_names(existing_app, updated)
    release_changed = release_signature(existing_app) != release_signature(updated)

    # Existing dates are authoritative unless the release itself changed.
    for key in core.DATE_FIELDS:
        updated.pop(key, None)
        if key in original_dates:
            updated[key] = original_dates[key]

    if release_changed:
        updated["versionDate"] = today
        updated["updatedAt"] = today
        # addedAt is intentionally never rewritten for an existing app.

    _preserve_version_item_dates(existing_app, updated, release_changed, today)
    drop_removed_fields(updated)

    return core.order_dict(updated, core.APP_FIELD_ORDER), changed


core.merge_existing_app = merge_existing_app


if __name__ == "__main__":
    try:
        core.main()
    except Exception as exc:
        print(f"❌ sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
