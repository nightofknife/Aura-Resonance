"""Small immutable profiles for navigation artwork shared by PC actions."""
from __future__ import annotations

from dataclasses import dataclass

import cv2


@dataclass(frozen=True)
class NavigationTemplateProfile:
    path: str
    roi: tuple[int, int, int, int]
    threshold: float
    use_grayscale: bool = True
    match_method: int = cv2.TM_CCOEFF_NORMED
    preprocess: str = "none"
    mask: str | None = None

    def match_kwargs(self) -> dict:
        return {"threshold": self.threshold, "use_grayscale": self.use_grayscale,
                "match_method": self.match_method, "preprocess": self.preprocess}

    def layout_spec(self) -> dict:
        # Loaders add resolved paths and normalize ROIs on their own copy.
        return {"path": self.path, "roi": list(self.roi), **self.match_kwargs()}


PROFILE_MENU = NavigationTemplateProfile(
    "templates/navigation/global/profile_entry.png", (305, 293, 215, 45), .85,
)
GLOBAL_BACK = NavigationTemplateProfile(
    "templates/navigation/global/back.png", (0, 0, 170, 80), .86,
)
GLOBAL_CITY_MAIN = NavigationTemplateProfile(
    "templates/navigation/global/city_main.png", (140, 0, 130, 80), .86,
)
CITY_PANEL_REPUTATION = NavigationTemplateProfile(
    "templates/navigation/city/panel_reputation.png", (21, 633, 85, 75), .86,
)
EXCHANGE_MENU = NavigationTemplateProfile(
    "templates/commerce/trade/shop/menu_ready.png", (720, 350, 220, 120), .86,
)
PRISON_DAILY_REPORT_HEADER = NavigationTemplateProfile(
    "templates/workshop/warehouse/controls/prison_daily_report_header.png", (248, 0, 325, 63), .92,
)
MIRAGE_ISLAND_HOME = NavigationTemplateProfile(
    "templates/commerce/investment/cape_island/controls/home_anchor.png", (0, 60, 1280, 660), .86,
)
MIRAGE_ISLAND_ADVOCACY = NavigationTemplateProfile(
    "templates/commerce/investment/cape_island/controls/advocacy_button.png", (1070, 570, 200, 135), .96,
    use_grayscale=False, match_method=cv2.TM_SQDIFF_NORMED,
)
MIRAGE_ISLAND_REVENUE = NavigationTemplateProfile(
    "templates/commerce/investment/cape_island/controls/revenue_overview_anchor.png", (610, 135, 300, 90), .86,
)
MIRAGE_ISLAND_INVESTMENT = NavigationTemplateProfile(
    "templates/commerce/investment/cape_island/controls/investment_page_anchor.png", (590, 130, 690, 570), .86,
)
MIRAGE_ISLAND_INVESTMENT_SUCCESS = NavigationTemplateProfile(
    "templates/commerce/investment/cape_island/controls/investment_success.png", (500, 180, 300, 350), .86,
)


def navigation_profile_layout(reference: str) -> dict:
    """Expand the one profile reference currently shared with JSON catalogs."""
    if reference != "navigation.profile":
        raise ValueError(f"Unknown navigation template profile: {reference}")
    return PROFILE_MENU.layout_spec()
