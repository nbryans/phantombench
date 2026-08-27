from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")


@dataclass
class ModelConfig:
    id: str
    provider: str
    model: str


@dataclass
class Config:
    repo_owner: str
    repo_name: str
    scrape: dict
    models: list[ModelConfig]
    review: dict
    demo: dict

    @property
    def repo_full_name(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    raw = yaml.safe_load(path.read_text())
    return Config(
        repo_owner=raw["repo"]["owner"],
        repo_name=raw["repo"]["name"],
        scrape=raw["scrape"],
        models=[ModelConfig(**m) for m in raw["models"]],
        review=raw["review"],
        demo=raw.get("demo", {}),
    )
