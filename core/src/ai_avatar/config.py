"""Config: アバタープロジェクトのYAML設定ファイル群を読み込む"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 読み込む設定ファイルの一覧（存在しない場合は空dictとして扱う）
_CONFIG_FILES = [
    "personality.yaml",
    "llm.yaml",
    "stt.yaml",
    "tts.yaml",
    "memory.yaml",
    "vision.yaml",
    "timeouts.yaml",
    "health.yaml",
]


class Config:
    """設定ファイル群を束ねて提供するオブジェクト。

    各YAMLファイルのトップレベルキーを属性として参照できる。
    例: config.llm["model"], config.tts["engine"]
    """

    def __init__(self, config_dir: str | Path) -> None:
        self._dir = Path(config_dir)
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        logger.info("Config dir (absolute): %s", self._dir.resolve())
        for filename in _CONFIG_FILES:
            path = self._dir / filename
            if not path.exists():
                logger.warning("Config not found (skipped): %s", path.resolve())
                continue
            with path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            # ファイル名（拡張子なし）をキーとして格納
            key = path.stem
            self._data[key] = data.get(key, data)
            logger.info("Loaded config: %s", path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getattr__(self, key: str) -> Any:
        try:
            return self._data[key]
        except KeyError:
            raise AttributeError(f"No config section '{key}' (no {key}.yaml or missing key)")
