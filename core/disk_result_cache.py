"""Versioned JSON snapshots; caller owns strict input/config keys and scheduling.

Comment factory closures cannot be serialized. Before saving, callers must store
complete rendered cards separately or as Paths in an explicit extra marker, then
honour that marker on reload. This module never runs factories or stores cookies.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from dataclasses import fields
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from .cache_policy import CACHE_RETENTION_SECONDS, reusable_card
from .data import (
    AudioContent, Author, DeliveryBatch, DeliveryPlan, DynamicContent, FileContent,
    GraphicsContent, ImageContent, ParseResult, Platform, VideoContent,
)


_TYPES = {cls.__name__: cls for cls in (
    AudioContent, Author, DeliveryBatch, DeliveryPlan, DynamicContent, FileContent,
    GraphicsContent, ImageContent, ParseResult, Platform, VideoContent,
)}
_SKIP = object()


def _matches(value, annotation) -> bool:
    if annotation is Any:
        return True
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        return any(_matches(value, item) for item in args)
    if origin is Literal:
        return value in args
    if origin is list:
        return isinstance(value, list) and all(_matches(item, args[0]) for item in value)
    if origin is dict:
        return isinstance(value, dict) and all(_matches(key, args[0]) and _matches(item, args[1]) for key, item in value.items())
    if annotation is float:
        return type(value) in (int, float)
    if annotation in (str, int, bool, type(None)):
        return type(value) is annotation
    if origin is not None:
        return False  # Futures are always materialized before persistence.
    return isinstance(value, annotation)


class _Codec:
    def __init__(self, root: Path):
        self.root = root
        self.encoded = {}
        self.decoded = {}
        self.active = set()

    def checked_path(self, value: Path, size: int | None = None) -> Path:
        path = value.resolve(strict=True)
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Media must be a file inside cache_dir")
        actual = path.stat().st_size
        if actual <= 0 or (size is not None and actual != size):
            raise ValueError("Missing, empty, or truncated media")
        if path.name.startswith("text_card_") and not reusable_card(path):
            raise ValueError("Expired generated card")
        return path

    async def encode(self, value, depth=0):
        if depth > 100:
            raise ValueError("Snapshot nesting too deep")
        if isinstance(value, asyncio.Future):
            try:
                value = await asyncio.shield(value)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                raise ValueError("Cancelled media") from None
            if not isinstance(value, Path):
                raise ValueError("Media future did not resolve to Path")
        if callable(value):
            return _SKIP
        if value is None or type(value) in (str, int, float, bool):
            return value
        if isinstance(value, Path):
            path = self.checked_path(value)
            return {"kind": "path", "value": str(path.relative_to(self.root)), "size": path.stat().st_size}
        ident = id(value)
        if ident in self.active:
            raise ValueError("Cyclic data")
        if ident in self.encoded:
            return {"kind": "ref", "id": self.encoded[ident]}
        self.active.add(ident)
        try:
            if type(value).__name__ in _TYPES and type(value) is _TYPES[type(value).__name__]:
                number = len(self.encoded)
                self.encoded[ident] = number
                data = {}
                for field in fields(value):
                    if field.init:
                        encoded = await self.encode(getattr(value, field.name), depth + 1)
                        if encoded is _SKIP:
                            raise ValueError("Callable dataclass field")
                        data[field.name] = encoded
                return {"kind": "data", "type": type(value).__name__, "id": number, "fields": data}
            if isinstance(value, dict):
                data = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError("Extra keys must be strings")
                    if "cookie" in key.casefold() or key == "_disk_result_key":
                        continue
                    if key == "_disk_card_path" and not reusable_card(item):
                        raise ValueError("Expired or invalid card marker")
                    encoded = await self.encode(item, depth + 1)
                    if encoded is not _SKIP:
                        data[key] = encoded
                return {"kind": "dict", "value": data}
            if isinstance(value, (list, tuple)):
                data = [await self.encode(item, depth + 1) for item in value]
                return {"kind": "list", "value": [item for item in data if item is not _SKIP]}
            raise ValueError("Unsupported snapshot value")
        finally:
            self.active.remove(ident)

    def decode(self, value, depth=0):
        if depth > 100:
            raise ValueError("Snapshot nesting too deep")
        if value is None or type(value) in (str, int, float, bool):
            return value
        kind = value["kind"]
        if kind == "path":
            relative = Path(value["value"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe path")
            if type(value["size"]) is not int or value["size"] <= 0:
                raise ValueError("Invalid file size")
            return self.checked_path(self.root / relative, value["size"])
        if kind == "ref":
            return self.decoded[value["id"]]
        if kind == "dict":
            data = {key: self.decode(item, depth + 1) for key, item in value["value"].items()}
            if "_disk_card_path" in data and not reusable_card(data["_disk_card_path"]):
                raise ValueError("Expired or invalid card marker")
            return data
        if kind == "list":
            return [self.decode(item, depth + 1) for item in value["value"]]
        if kind == "data":
            cls = _TYPES[value["type"]]
            data = {key: self.decode(item, depth + 1) for key, item in value["fields"].items()}
            expected = {field.name for field in fields(cls) if field.init}
            if data.keys() != expected or value["id"] in self.decoded:
                raise ValueError("Invalid dataclass schema")
            hints = get_type_hints(cls)
            if any(not _matches(item, hints[key]) for key, item in data.items()):
                raise ValueError("Invalid dataclass field type")
            result = cls(**data)
            self.decoded[value["id"]] = result
            return result
        raise ValueError("Unknown snapshot kind")


class DiskResultCache:
    VERSION = 1
    TTL_SECONDS = CACHE_RETENTION_SECONDS

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir).resolve()
        self.directory = self.cache_dir / "disk_results"

    def _path(self, key: str) -> Path:
        if not isinstance(key, str) or not key:
            raise ValueError("A nonempty caller-supplied cache key is required")
        path = self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        if self.directory.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(self.cache_dir):
            raise ValueError("Unsafe manifest path")
        return path

    async def save(self, key: str, result: ParseResult) -> bool:
        temporary = None
        try:
            path = self._path(key)
            if not isinstance(result, ParseResult) or result.extra.get("cache_result") is False:
                return False
            payload = await _Codec(self.cache_dir).encode(result)
            _Codec(self.cache_dir).decode(payload)
            manifest = {"version": self.VERSION, "written_at": time.time(), "result": payload}
            data = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode()
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=self.directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return True
        except Exception:
            return False
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def load(self, key: str) -> ParseResult | None:
        try:
            manifest = json.loads(self._path(key).read_bytes())
            if manifest["version"] != self.VERSION or not 0 <= time.time() - manifest["written_at"] < self.TTL_SECONDS:
                return None
            result = _Codec(self.cache_dir).decode(manifest["result"])
            return result if isinstance(result, ParseResult) else None
        except Exception:
            return None
