"""Render station navigation artwork from local game sprites without taking screenshots."""
from __future__ import annotations

import argparse
from functools import lru_cache
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

import build_daily_reward_templates as native


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "plans/resonance_pc/templates/navigation/station_main"
DAILY_PREFAB = "ui/stagequest/stagequest.asset"
DAILY_ACTIVITY = "/StageQuest/Group_Daily/Group_Activity"
SOURCES = {
    "city_panel_reputation": ("ui/citymap/group_reputation.asset", "/Group_Reputation/Btn_Reputation"),
    "passenger_entry": ("ui/mainui/mainui.asset", "/MainUI/Group_OutSide/Group_Station/Btn_HandleG"),
    "battle_terminal_entry": ("ui/mainui/mainui.asset", "/MainUI/Group_Common/Btn_Safe"),
    "battle_terminal_logo": ("ui/activity/activitysafe/activitysafe.asset", "/ActivitySafe/Group_Top/Img_"),
    "daily_activity_badge": (DAILY_PREFAB, f"{DAILY_ACTIVITY}/Img_Icon"),
    "battle_pass_standard_handbook": (
        "ui/bp_quest/battlepass_quest.asset", "/BattlePass_Quest/Group_Reward_Quest/Group_BattlePass/Btn_BpFree",
    ),
}


def _circle(image, box):
    mask = Image.new("L", image.size)
    ImageDraw.Draw(mask).ellipse(box, fill=255)
    image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
    return image, mask


def _opaque_mask(image):
    return image.getchannel("A").point(lambda value: 255 if value >= 224 else 0)


def _render_daily_badge(icon, background):
    # The prefab places Img_Icon at (123, 36) from the top-left of Img_Bg.
    background.alpha_composite(icon, (123, 36))
    # The badge tip is above row 88 at client scale, where Txt_Num starts.
    image = background.resize((1040, 143), Image.Resampling.LANCZOS).crop((55, 15, 180, 88))
    return image, _opaque_mask(image)


def _render(key, image):
    if key == "passenger_entry":
        image = image.resize((61, 61), Image.Resampling.LANCZOS)
        return image, _opaque_mask(image)
    if key == "battle_terminal_entry":
        image = image.resize((333, 78), Image.Resampling.BILINEAR).crop((13, 10, 60, 57))
        return _circle(image, (0, 0, 46, 46))

    image = image.resize(tuple(round(side * 2 / 3) for side in image.size), Image.Resampling.LANCZOS)
    if key == "battle_terminal_logo":
        return _circle(image.crop((8, 7, 114, 113)), (1, 1, 105, 105))
    mask = _opaque_mask(image)
    if key == "city_panel_reputation":
        # The prefab's red-dot center is approximately (53, 4) at client scale.
        # Exclude the dot radius and a margin for antialiasing and alignment.
        box = (38, 0, image.width - 1, 19)
        ImageDraw.Draw(mask).rectangle(box, fill=0)
        alpha = image.getchannel("A")
        ImageDraw.Draw(alpha).rectangle(box, fill=0)
        image.putalpha(alpha)
    return image, mask


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path, required=True)
    parser.add_argument("--pythonlibs", type=Path)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--provenance", type=Path)
    args = parser.parse_args()
    native.configure(args)

    manifest = next(obj.read_typetree() for obj in native.bundle("Asset").objects
                    if obj.type.name == "AssetBundleManifest")
    names = dict(manifest["AssetBundleNames"])
    infos = dict(manifest["AssetBundleInfos"])
    indices = {name: index for index, name in names.items()}
    prefab = lru_cache(maxsize=None)(native.Prefab)

    def read_sprite(relative, node):
        source = prefab(relative)
        sprite_id = source.img(node)["m_Sprite"]["m_PathID"]
        dependencies = infos[indices[relative]]["AssetBundleDependencies"]
        for bundle in (relative, *(names[index] for index in dependencies)):
            if sprite_id in native.ASSETS:
                break
            native.asset_table(bundle)
        image, asset = source.image(node)
        return image, {**asset, "bundle_sha256": native.SOURCE[asset["bundle"]]["sha256"]}

    report = {}
    args.output.mkdir(parents=True, exist_ok=True)
    for key, (relative, node) in SOURCES.items():
        image, asset = read_sprite(relative, node)
        if key == "daily_activity_badge":
            background, asset["background"] = read_sprite(DAILY_PREFAB, f"{DAILY_ACTIVITY}/Img_Bg")
            image, mask = _render_daily_badge(image, background)
        else:
            image, mask = _render(key, image)
        template_path = args.output / f"{key}.png"
        mask_path = args.output / f"{key}.mask.png"
        image.save(template_path)
        mask.save(mask_path)
        report[key] = {**asset, "rendered_size": list(image.size), "template_sha256": native.sha(template_path),
                       "mask_sha256": native.sha(mask_path)}

    if args.provenance:
        args.provenance.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
