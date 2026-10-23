"""Загрузка конфигурации бота (config.yaml).

В config.yaml — только глобальные настройки (маркер, валюта, ручная
кладь, параметры нагрузки и здоровья). Персональные маршруты, даты и
цены каждый пользователь задаёт сам командой /watch.
"""
import yaml
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path=None):
    with open(path or DEFAULT_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)
