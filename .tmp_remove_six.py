#!/usr/bin/env python3
import json
from pathlib import Path

REMOVE_IDS = {
    "APP-5340789548",  # Scrl Photo Collage Maker
    "APP-8248216047",  # Soundlab Audio Editor
    "APP-2162381647",  # Picsart Pro
    "APP-1605881895",  # Remini Pro
    "APP-3760722823",  # Lightroom
    "APP-6253966381",  # VSCO
}

for filename in ("IPA-AR.json", "IPA-EN.json"):
    path = Path(filename)
    data = json.loads(path.read_text(encoding="utf-8"))
    apps = data.get("apps", [])
    if len(apps) != 1006:
        raise SystemExit(f"{filename}: expected 1006 apps before temporary removal, got {len(apps)}")

    found = {str(app.get("id", "")).strip() for app in apps if isinstance(app, dict)} & REMOVE_IDS
    missing = sorted(REMOVE_IDS - found)
    if missing:
        raise SystemExit(f"{filename}: missing requested ids: {missing}")

    data["apps"] = [
        app for app in apps
        if not (isinstance(app, dict) and str(app.get("id", "")).strip() in REMOVE_IDS)
    ]

    if len(data["apps"]) != 1000:
        raise SystemExit(f"{filename}: expected 1000 apps after temporary removal, got {len(data['apps'])}")

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{filename}: 1000 apps")
