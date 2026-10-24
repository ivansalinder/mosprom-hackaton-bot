from pathlib import Path

from monitor.config import settings


def find_asset(asset_name: str) -> Path | None:
    asset_path = settings.resolved_assets_dir / asset_name
    return asset_path if asset_path.is_file() else None


def asset_stub(asset_name: str) -> str:
    labels = {
        "postcard_1.png": "Открытка №1",
        "postcard_2.png": "Открытка №2",
        "presentation.pdf": "корпоративная презентация о компании",
        "adaptation_plan.pdf": "Адаптационный план",
        "events_1.jpg": "ФОТО с мероприятий",
        "events_2.jpg": "ФОТО с мероприятий",
        "self_intro_example.png": "образец самопредставления",
    }
    if asset_name.startswith("img_") and asset_name.endswith(".png"):
        label = f"Изображение {asset_name[4:-4]}"
    elif asset_name.startswith("lunch_") and asset_name.endswith(".pdf"):
        label = "PDF с перечнем вкусных заведений"
    else:
        label = labels.get(asset_name, asset_name)
    return f"📎 [TODO: {label}]"
