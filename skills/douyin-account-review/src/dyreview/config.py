"""配置加载：YAML/JSON + `${ENV}` 展开 + 按场景校验必填项。

约定
----
* 取值统一走 `cfg.get("douyin.token.refresh_before_ratio", 0.3)`，点号路径。
* `${VAR}` 未设置时展开为空字符串并记录，真正需要时由 `require()` 报错，
  这样 handoff 模式（不调大模型）不会因为缺 LLM_API_KEY 而整体失败。
* 配置文件查找顺序：显式入参 > $DYREVIEW_CONFIG > config/config.yaml
  > config/config.example.yaml（兜底，仅用于 demo/自检，会打警告）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .errors import ConfigError

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

SKILL_ROOT = Path(__file__).resolve().parents[2]

_MISSING = object()


def _expand(value: Any, unresolved: set[str]) -> Any:
    """递归展开字符串里的 ${VAR} 与 ${VAR:-default}。"""
    if isinstance(value, str):
        # 整串就是一个变量时保留原始类型意识：展开后仍是字符串
        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            raw = os.environ.get(name)
            if raw is None or raw == "":
                if default is not None:
                    return default
                if raw is None:
                    unresolved.add(name)
                return ""
            return raw

        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v, unresolved) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, unresolved) for v in value]
    return value


def _read_file(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # 延迟导入，纯逻辑测试不需要 PyYAML
        except ImportError as exc:  # pragma: no cover
            raise ConfigError("读取 YAML 配置需要 PyYAML，请 pip install PyYAML") from exc
        data = yaml.safe_load(text) or {}
    elif suffix == ".json":
        data = json.loads(text)
    else:
        raise ConfigError(f"不支持的配置文件类型：{path.suffix}（用 .yaml/.yml/.json）")
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件根节点必须是映射：{path}")
    return data


class Config:
    """轻量配置对象：点号取值 + 环境变量展开 + 场景校验。"""

    def __init__(self, data: dict, *, source: str = "<inline>", is_example: bool = False,
                 root: Path | None = None) -> None:
        unresolved: set[str] = set()
        self._data: dict = _expand(data, unresolved)
        self.unresolved_env: set[str] = unresolved
        self.source = source
        self.is_example = is_example
        self.root = Path(root) if root else SKILL_ROOT

    # ------------------------------------------------------------------ 读取
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def get_int(self, dotted: str, default: int) -> int:
        try:
            return int(self.get(dotted, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, dotted: str, default: float) -> float:
        try:
            return float(self.get(dotted, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, dotted: str, default: bool = False) -> bool:
        val = self.get(dotted, default)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "y", "on")

    def section(self, dotted: str) -> dict:
        val = self.get(dotted, {})
        return val if isinstance(val, dict) else {}

    def require(self, dotted: str, hint: str = "") -> str:
        """取必填字符串；缺失时抛出带修复提示的 ConfigError。"""
        val = self.get(dotted, "")
        if isinstance(val, str) and val.strip():
            return val.strip()
        env_hint = f"（对应环境变量是否已注入？未解析：{sorted(self.unresolved_env)}）" \
            if self.unresolved_env else ""
        if self.is_example:
            env_hint += "（当前用的是 config.example.yaml，请复制为 config/config.yaml）"
        raise ConfigError(f"缺少必填配置项 {dotted}{('：' + hint) if hint else ''}{env_hint}")

    def path(self, dotted_or_rel: str) -> Path:
        """把配置里的相对路径解析到 Skill 根目录下。"""
        raw = self.get(dotted_or_rel)
        rel = raw if isinstance(raw, str) and raw.strip() else dotted_or_rel
        p = Path(rel)
        return p if p.is_absolute() else (self.root / p)

    @property
    def var_dir(self) -> Path:
        return self.path("paths.var_dir") if self.get("paths.var_dir") else self.root / "var"

    @property
    def schema(self) -> str:
        return str(self.get("database.schema", "dy") or "dy")

    @property
    def timezone(self) -> str:
        return str(self.get("skill.timezone", "Asia/Shanghai"))

    # ------------------------------------------------------------------ 校验
    def validate(self, needs: Iterable[str]) -> None:
        """按使用场景校验必填项。

        needs 取值：db / douyin / feishu / llm
        """
        needs = set(needs)
        if "db" in needs:
            self.require("database.dsn", "193 服务器 PostgreSQL 连接串")
        if "douyin" in needs:
            self.require("douyin.client_key", "开放平台应用 client_key")
            self.require("douyin.client_secret", "开放平台应用 client_secret")
        if "feishu" in needs:
            if not self.get("feishu.webhook_url"):
                self.require("feishu.app_id", "飞书自建应用 app_id")
                self.require("feishu.app_secret", "飞书自建应用 app_secret")
                self.require("feishu.default_chat_id", "默认回推会话 chat_id")
        if "llm" in needs and self.get("llm.mode", "handoff") == "builtin":
            self.require("llm.base_url", "OpenAI 兼容接口 base_url")
            self.require("llm.api_key", "大模型 api_key")


def load_config(path: str | Path | None = None, *, root: Path | None = None) -> Config:
    """按优先级加载配置。"""
    skill_root = Path(root) if root else SKILL_ROOT
    explicit = path or os.environ.get("DYREVIEW_CONFIG")

    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = (skill_root / p).resolve() if not p.exists() else p.resolve()
        return Config(_read_file(p), source=str(p), root=skill_root)

    primary = skill_root / "config" / "config.yaml"
    if primary.exists():
        return Config(_read_file(primary), source=str(primary), root=skill_root)

    example = skill_root / "config" / "config.example.yaml"
    if example.exists():
        return Config(_read_file(example), source=str(example), is_example=True, root=skill_root)

    raise ConfigError(
        "未找到配置文件。请 cp config/config.example.yaml config/config.yaml，"
        "或用 --config 指定路径 / 设置 DYREVIEW_CONFIG。"
    )


def config_from_dict(data: dict, *, root: Path | None = None) -> Config:
    """测试与内嵌调用用。"""
    return Config(data, source="<dict>", root=root)
