import json
import queue
import re
import threading
import tkinter as tk
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps, ImageTk


PREVIEW_EMPTY_TEXT = "Select an image to preview it here."


class UiDispatchMixin:
    def _init_ui_dispatcher(self) -> None:
        self._ui_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._ui_dispatch_after_id: str | None = None
        self._schedule_ui_drain()

    def _post_to_ui(self, callback: Callable[[], None]) -> None:
        self._ui_queue.put(callback)

    def _schedule_ui_drain(self) -> None:
        if self._ui_dispatch_after_id is None:
            self._ui_dispatch_after_id = self.after(16, self._drain_ui_queue)

    def _drain_ui_queue(self) -> None:
        self._ui_dispatch_after_id = None
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                pass
        if self.winfo_exists():
            self._schedule_ui_drain()

    def _shutdown_ui_dispatcher(self) -> None:
        if self._ui_dispatch_after_id is not None:
            try:
                self.after_cancel(self._ui_dispatch_after_id)
            except Exception:
                pass
            self._ui_dispatch_after_id = None


class StateStore:
    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file

    def load(self) -> tuple[dict[str, object], str | None]:
        if not self.state_file.exists():
            return {}, None
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            return {}, f"Could not read saved state: {exc}"
        if not isinstance(payload, dict):
            return {}, "Saved state has unexpected format."
        return payload, None

    def save(self, payload: dict[str, object]) -> str | None:
        try:
            self.state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return None
        except Exception as exc:
            return f"Could not save state: {exc}"


class ImageStateHelper:
    @staticmethod
    def normalize_tags(tags_raw: object) -> list[str]:
        if not isinstance(tags_raw, list):
            return []
        return sorted({str(tag).strip().lower() for tag in tags_raw if str(tag).strip()})

    @staticmethod
    def normalized_review_status(value: object) -> str:
        status = str(value or "").strip().lower()
        return "reject" if status == "reject" else "unreviewed"

    @classmethod
    def prune_image_state(
        cls,
        image_state: dict[str, dict[str, object]],
        existing_keys: set[str] | None = None,
    ) -> dict[str, dict[str, object]]:
        pruned: dict[str, dict[str, object]] = {}
        for path_key, state in image_state.items():
            if existing_keys is not None and path_key not in existing_keys:
                continue
            favorite = bool(state.get("favorite", False))
            tags = cls.normalize_tags(state.get("tags", []))
            review_status = str(state.get("review_status", "")).strip().lower()
            if not favorite and not tags and review_status != "reject":
                continue
            item: dict[str, object] = {"favorite": favorite, "tags": tags}
            if review_status == "reject":
                item["review_status"] = review_status
            pruned[path_key] = item
        return pruned


class CatalogHelper:
    @staticmethod
    def metadata_signature(path: Path, parse_rev: int) -> tuple[int, int, int]:
        try:
            stat = path.stat()
            return int(stat.st_mtime_ns), int(stat.st_size), parse_rev
        except Exception:
            return -1, -1, parse_rev

    @staticmethod
    def build_search_record(path: Path, metadata: dict[str, str], tags: list[str]) -> tuple[str, str]:
        prompt_text = str(metadata.get("Prompt", "")).strip().lower()
        haystack_parts = [path.name.lower()]
        if tags:
            haystack_parts.append(" ".join(tags))
        for field in ("Prompt", "Negative prompt", "Model", "Sampler", "Scheduler", "Seed", "CFG", "Steps", "Resolution", "LoRAs"):
            value = str(metadata.get(field, "")).strip().lower()
            if value:
                haystack_parts.append(value)
        return prompt_text, " | ".join(haystack_parts)

    @staticmethod
    def best_root_for_path(path: Path, roots: list[Path]) -> Path | None:
        best: Path | None = None
        for root in roots:
            try:
                path.relative_to(root)
            except Exception:
                continue
            if best is None or len(root.parts) > len(best.parts):
                best = root
        return best

    @staticmethod
    def safe_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except Exception:
            return 0.0

    @staticmethod
    def file_signature(path: Path) -> tuple[int, int]:
        try:
            stat = path.stat()
            return int(stat.st_mtime_ns), int(stat.st_size)
        except Exception:
            return -1, -1

    @staticmethod
    def prune_runtime_caches(
        existing: set[str],
        metadata_cache: dict[str, dict[str, str]],
        metadata_cache_sig: dict[str, tuple[int, int, int]],
        search_index_cache: dict[str, tuple[tuple[int, int, int, str], str, str]],
        thumbnail_cache: OrderedDict[tuple[str, int, int, int], ImageTk.PhotoImage],
    ) -> tuple[
        dict[str, dict[str, str]],
        dict[str, tuple[int, int, int]],
        dict[str, tuple[tuple[int, int, int, str], str, str]],
        OrderedDict[tuple[str, int, int, int], ImageTk.PhotoImage],
    ]:
        return (
            {k: v for k, v in metadata_cache.items() if k in existing},
            {k: v for k, v in metadata_cache_sig.items() if k in existing},
            {k: v for k, v in search_index_cache.items() if k in existing},
            OrderedDict((k, v) for k, v in thumbnail_cache.items() if k[0] in existing),
        )

    @staticmethod
    def scan_image_roots(
        roots: list[Path],
        supported_extensions: set[str],
        should_cancel: Callable[[], bool] | None = None,
    ) -> tuple[list[Path], dict[str, Path], dict[str, float], bool]:
        found_by_key: dict[str, Path] = {}
        root_by_key: dict[str, Path] = {}
        mtime_by_key: dict[str, float] = {}

        for root in roots:
            if should_cancel and should_cancel():
                return [], {}, {}, True
            for file_path in root.rglob("*"):
                if should_cancel and should_cancel():
                    return [], {}, {}, True
                try:
                    if not file_path.is_file() or file_path.suffix.lower() not in supported_extensions:
                        continue
                except Exception:
                    continue
                mtime: float | None = None
                try:
                    mtime = float(file_path.stat().st_mtime)
                except Exception:
                    pass
                try:
                    canonical = file_path.resolve()
                except Exception:
                    canonical = file_path
                key = str(canonical)
                if key not in found_by_key:
                    found_by_key[key] = canonical
                    if mtime is not None:
                        mtime_by_key[key] = mtime
                elif mtime is not None and mtime > mtime_by_key.get(key, 0.0):
                    mtime_by_key[key] = mtime
                prev_root = root_by_key.get(key)
                if prev_root is None or len(root.parts) > len(prev_root.parts):
                    root_by_key[key] = root

        found_keys = sorted(found_by_key.keys(), key=lambda key: mtime_by_key.get(key, 0.0), reverse=True)
        found = [found_by_key[key] for key in found_keys]
        return found, root_by_key, mtime_by_key, False

    @staticmethod
    def filter_paths(
        paths: list[Path],
        *,
        query: str,
        tag_filter: str,
        favorites_only: bool,
        review_filter: str,
        sort_mode: str,
        get_state: Callable[[Path], dict[str, object]],
        get_review_status: Callable[[Path], str],
        get_search_record: Callable[[Path], tuple[str, str]],
        sort_key: Callable[[Path], float],
        should_cancel: Callable[[], bool] | None = None,
    ) -> tuple[list[Path], bool]:
        filtered: list[Path] = []
        for path in paths:
            if should_cancel and should_cancel():
                return [], True
            state = get_state(path)
            favorite = bool(state.get("favorite", False))
            review_status = get_review_status(path)

            if favorites_only and not favorite:
                continue
            if review_filter == "Unreviewed" and review_status != "unreviewed":
                continue
            if review_filter == "Reject" and review_status != "reject":
                continue
            if tag_filter or query:
                prompt_text, search_text = get_search_record(path)
                if tag_filter and tag_filter not in prompt_text:
                    continue
                if query and query not in search_text:
                    continue
            filtered.append(path)

        reverse = sort_mode != "Oldest"
        filtered.sort(key=sort_key, reverse=reverse)
        return filtered, False


class ReviewHelper:
    @staticmethod
    def review_display(status: str, palette: dict[str, str]) -> tuple[str, str, str]:
        normalized = ImageStateHelper.normalized_review_status(status)
        if normalized == "reject":
            return "Reject", "#fdebec", "#b64b5f"
        return "Unreviewed", palette["chip_soft_bg"], palette["chip_soft_fg"]

    @staticmethod
    def review_thumb_palette(review_status: str, palette: dict[str, str]) -> tuple[str, str]:
        normalized = ImageStateHelper.normalized_review_status(review_status)
        if normalized == "reject":
            return "#feebef", "#eab6c0"
        return palette["thumb_bg"], palette["border"]

    @staticmethod
    def next_review_focus_path(current_path: Path | None, image_paths: list[Path]) -> Path | None:
        if current_path is None or current_path not in image_paths:
            return None
        idx = image_paths.index(current_path)
        if idx + 1 < len(image_paths):
            return image_paths[idx + 1]
        if idx - 1 >= 0:
            return image_paths[idx - 1]
        return None

    @staticmethod
    def next_path_after_deletion(
        image_paths: list[Path],
        current_image_path: Path | None,
        deleted_keys: set[str],
    ) -> Path | None:
        if not image_paths:
            return None
        current = current_image_path
        if current is None:
            for path in image_paths:
                if str(path) not in deleted_keys:
                    return path
            return None
        try:
            idx = image_paths.index(current)
        except ValueError:
            idx = -1
        candidates = image_paths[idx + 1 :] + image_paths[:idx] if idx >= 0 else list(image_paths)
        for path in candidates:
            if str(path) not in deleted_keys:
                return path
        return None


class MetadataParser:
    PICK_MAP = {
        "Model": [
            "Model",
            "model",
            "model_name",
            "sd_model_name",
            "sd_model_hash",
            "ckpt_name",
            "unet_name",
            "checkpoint",
            "checkpoint_name",
        ],
        "Prompt": ["Prompt", "prompt_text", "prompt"],
        "Negative prompt": ["Negative prompt", "negative_prompt", "negative"],
        "Sampler": ["Sampler", "sampler", "sampler_name"],
        "Scheduler": ["Scheduler", "scheduler", "Schedule type"],
        "CFG": ["CFG", "CFG scale", "cfg", "cfg_scale"],
        "Seed": ["Seed", "seed"],
        "Steps": ["Steps", "steps"],
        "Resolution": ["Resolution", "Size", "size"],
    }

    @classmethod
    def extract_metadata(cls, path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        try:
            with Image.open(path) as im:
                result["_width"] = str(im.width)
                result["_height"] = str(im.height)
                for key, value in (im.info or {}).items():
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", errors="replace")
                    result[str(key)] = str(value)
                exif = im.getexif()
                if exif:
                    for tag_id, value in exif.items():
                        result[f"EXIF_{tag_id}"] = str(value)
        except Exception as exc:
            result["_error"] = str(exc)

        normalized = cls.extract_generation_fields(result)
        result.update(normalized)
        return result

    @classmethod
    def extract_generation_fields(cls, raw: dict[str, str]) -> dict[str, str]:
        parsed: dict[str, str] = {}

        for key in ("parameters", "Comment", "comment", "UserComment"):
            if key not in raw:
                continue
            text = str(raw[key])
            if "Negative prompt:" in text or "Steps:" in text:
                prompt, neg, tail = cls.parse_sd_parameters(text)
                if prompt:
                    parsed.setdefault("Prompt", prompt)
                if neg:
                    parsed.setdefault("Negative prompt", neg)
                parsed.update({k: v for k, v in tail.items() if k not in parsed})

        prompt_json = raw.get("prompt")
        if prompt_json and str(prompt_json).lstrip().startswith("{"):
            comfy = cls.parse_comfy_prompt_json(str(prompt_json))
            parsed.update({k: v for k, v in comfy.items() if v and k not in parsed})

        for key in ("workflow", "extra_pnginfo"):
            block = raw.get(key)
            if not block:
                continue
            text = str(block)
            if text.lstrip().startswith("{"):
                comfy2 = cls.parse_comfy_prompt_json(text)
                parsed.update({k: v for k, v in comfy2.items() if v and k not in parsed})
            model_guess = cls.guess_model_from_text(text)
            if model_guess and "Model" not in parsed:
                parsed["Model"] = model_guess

        for target, keys in cls.PICK_MAP.items():
            for key in keys:
                if key in parsed and parsed[key]:
                    value = str(parsed[key])
                    if target == "Model":
                        value = cls.normalize_model_value(value)
                        if not value:
                            continue
                    parsed[target] = value
                    break
                if key in raw and raw[key]:
                    value = str(raw[key])
                    if target == "Model":
                        value = cls.normalize_model_value(value)
                        if not value:
                            continue
                    parsed[target] = value
                    break

        loras_found: set[str] = set()
        excluded_assets: set[str] = set()

        for key in ("prompt", "workflow", "extra_pnginfo"):
            value = raw.get(key)
            if value:
                value_text = str(value)
                loras_found.update(cls.extract_loras_from_workflow_json(value_text))
                excluded_assets.update(cls.extract_model_assets_from_workflow_json(value_text))
                excluded_assets.update(cls.extract_non_lora_assets_from_text(value_text))

        for key in (
            "LoRAs",
            "loras",
            "lora",
            "Lora hashes",
            "parameters",
            "Comment",
            "comment",
            "UserComment",
        ):
            value = raw.get(key)
            if value:
                value_text = str(value)
                loras_found.update(cls.extract_loras_from_text(value_text))
                excluded_assets.update(cls.extract_non_lora_assets_from_text(value_text))

        prompt_norm = parsed.get("Prompt", "")
        if prompt_norm:
            loras_found.update(cls.extract_loras_from_text(prompt_norm))
            excluded_assets.update(cls.extract_non_lora_assets_from_text(prompt_norm))

        model_hint = cls.clean_lora_name(parsed.get("Model", ""))
        if model_hint:
            excluded_assets.add(model_hint)

        if loras_found:
            canonical_excluded = {cls.canonical_asset_name(item) for item in excluded_assets if item}
            loras_found = {
                item
                for item in loras_found
                if cls.canonical_asset_name(item) not in canonical_excluded and not cls.looks_like_non_lora_asset(item)
            }
            if loras_found:
                parsed["LoRAs"] = ", ".join(sorted(loras_found, key=str.lower))

        return parsed

    @classmethod
    def parse_comfy_prompt_json(cls, text: str) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                return out
            if "prompt" in data and isinstance(data["prompt"], dict):
                data = data["prompt"]

            clip_texts: list[str] = []
            loras: list[str] = []
            for node in data.values():
                if not isinstance(node, dict):
                    continue
                cls_name = str(node.get("class_type", ""))
                inputs = node.get("inputs", {})
                if not isinstance(inputs, dict):
                    continue

                if cls_name == "CLIPTextEncode" and isinstance(inputs.get("text"), str):
                    clip_texts.append(inputs["text"].strip())

                if "KSampler" in cls_name:
                    out.setdefault("Sampler", str(inputs.get("sampler_name", "")))
                    out.setdefault("Scheduler", str(inputs.get("scheduler", "")))
                    out.setdefault("Seed", str(inputs.get("seed", "")))
                    out.setdefault("CFG", str(inputs.get("cfg", "")))
                    out.setdefault("Steps", str(inputs.get("steps", "")))

                if "CheckpointLoader" in cls_name or "UNETLoader" in cls_name or "DiffusionModelLoader" in cls_name:
                    for key in ("ckpt_name", "unet_name", "model_name", "checkpoint_name", "checkpoint", "model"):
                        value = cls.normalize_model_value(str(inputs.get(key, "")).strip())
                        if value:
                            out.setdefault("Model", value)
                            break

                if "EmptyLatentImage" in cls_name:
                    w = str(inputs.get("width", "")).strip()
                    h = str(inputs.get("height", "")).strip()
                    if w and h:
                        out.setdefault("Resolution", f"{w}x{h}")

                if "lora" in cls_name.lower():
                    for key, value in inputs.items():
                        if not isinstance(value, str):
                            continue
                        key_low = str(key).lower()
                        if re.fullmatch(r"lora(_\d+)?_name", key_low) or key_low in {"lora_name", "lora_file", "lora_path", "lora"}:
                            name = cls.clean_lora_name(value)
                            if name and not cls.looks_like_non_lora_asset(name):
                                loras.append(name)

                if "Model" not in out:
                    for key in ("ckpt_name", "unet_name", "model_name", "checkpoint_name", "checkpoint", "model"):
                        value = cls.normalize_model_value(str(inputs.get(key, "")).strip())
                        if value:
                            out["Model"] = value
                            break

            if clip_texts:
                out.setdefault("Prompt", clip_texts[0])
            if len(clip_texts) > 1:
                out.setdefault("Negative prompt", clip_texts[1])
            if loras:
                out.setdefault("LoRAs", ", ".join(sorted(set(loras), key=str.lower)))
            else:
                lora_guess = cls.extract_loras_from_json_obj(data)
                if lora_guess:
                    out.setdefault("LoRAs", ", ".join(sorted(lora_guess, key=str.lower)))
            if "Model" not in out:
                model_guess = cls.guess_model_from_text(text)
                if model_guess:
                    out["Model"] = model_guess
        except Exception:
            return out
        return out

    @staticmethod
    def guess_model_from_text(text: str) -> str:
        match = re.search(r"([A-Za-z0-9_./\\-]+\.(?:safetensors|ckpt|pth))", text, flags=re.IGNORECASE)
        return match.group(1) if match else ""

    @staticmethod
    def normalize_model_value(value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        if re.fullmatch(r"\[\s*'?\d+'?\s*,\s*\d+\s*\]", value):
            return ""
        if value.lower() in {"none", "null", "[]"}:
            return ""
        return value

    @staticmethod
    def clean_lora_name(value: str) -> str:
        value = (value or "").strip().strip("\"'")
        if not value:
            return ""
        if re.fullmatch(r"\[\s*'?\d+'?\s*,\s*\d+\s*\]", value):
            return ""
        value = value.replace("\\", "/")
        if "/" in value:
            value = value.split("/")[-1]
        if ":" in value and not value.lower().endswith(":safetensors"):
            left = value.split(":", 1)[0].strip()
            if left:
                value = left
        if value.lower().endswith(".safetensors"):
            value = value[:-12]
        return value.strip()

    @classmethod
    def extract_loras_from_text(cls, text: str) -> set[str]:
        found: set[str] = set()
        if not text:
            return found
        maybe_json = text.lstrip()
        if maybe_json.startswith("{") and maybe_json.endswith("}"):
            try:
                found.update(cls.extract_loras_from_json_obj(json.loads(maybe_json)))
            except Exception:
                pass
        for match in re.findall(r"<lora:([^:>]+)(?::[^>]+)?>", text, flags=re.IGNORECASE):
            name = cls.clean_lora_name(match)
            if name:
                found.add(name)
        for match in re.findall(r'(?i)"(?:lora(?:_[0-9]+)?_name|lora_name|lora_file|lora_path)"\s*:\s*"([^"]+)"', text):
            name = cls.clean_lora_name(match)
            if name:
                found.add(name)
        for match in re.findall(r"(?i)\b(?:lora(?:_[0-9]+)?_name|lora_name|lora_file|lora_path)\s*[:=]\s*([^\n\r,]+)", text):
            name = cls.clean_lora_name(match)
            if name:
                found.add(name)
        for block in re.findall(r"(?i)Lora hashes\s*:\s*([^\n\r]+)", text):
            for part in block.split(","):
                name = cls.clean_lora_name(part.split(":", 1)[0].strip())
                if name:
                    found.add(name)
        return found

    @classmethod
    def extract_loras_from_json_obj(cls, obj: object, in_lora_context: bool = False) -> set[str]:
        found: set[str] = set()
        if isinstance(obj, dict):
            class_type = str(obj.get("class_type", "")).lower()
            context_here = in_lora_context or ("lora" in class_type)
            for key, value in obj.items():
                key_low = str(key).lower()
                child_context = context_here or "lora" in key_low or key_low.startswith("add_lora")
                if isinstance(value, str):
                    if cls.looks_like_lora_value(value, key_low, child_context):
                        name = cls.clean_lora_name(value)
                        if name and not cls.looks_like_non_lora_asset(name):
                            found.add(name)
                elif isinstance(value, (dict, list)):
                    found.update(cls.extract_loras_from_json_obj(value, child_context))
        elif isinstance(obj, list):
            for item in obj:
                found.update(cls.extract_loras_from_json_obj(item, in_lora_context))
        return found

    @staticmethod
    def looks_like_lora_value(value: str, key_low: str, in_lora_context: bool) -> bool:
        v = (value or "").strip()
        if not v or re.fullmatch(r"\[\s*'?\d+'?\s*,\s*\d+\s*\]", v):
            return False
        if v.lower() in {"none", "null", "false", "true"}:
            return False
        lv = v.lower()
        is_safetensors = lv.endswith(".safetensors")
        key_model_like = any(
            token in key_low
            for token in ("model", "ckpt", "checkpoint", "unet", "base_model", "diffusion_model", "vae", "clip", "text_encoder", "textencoder", "tokenizer", "encoder")
        )
        non_lora_name = MetadataParser.looks_like_non_lora_asset(v)
        if "lora" in key_low and (is_safetensors or "." not in lv):
            return True
        if in_lora_context and is_safetensors and not key_model_like and not non_lora_name:
            return True
        if is_safetensors and any(token in key_low for token in ("name", "file", "path")) and not key_model_like and not non_lora_name:
            return True
        return False

    @staticmethod
    def looks_like_non_lora_asset(value: str) -> bool:
        v = (value or "").strip().lower().replace("\\", "/")
        if not v:
            return False
        if v.endswith(".safetensors"):
            v = v[:-12]
        name = v.split("/")[-1]
        return any(token in name for token in ("vae", "text_encoder", "text-encoder", "clip", "tokenizer", "unet", "checkpoint", "ckpt", "diffusion_model", "image_vae", "textencoder"))

    @classmethod
    def extract_non_lora_assets_from_text(cls, text: str) -> set[str]:
        found: set[str] = set()
        if not text:
            return found
        maybe_json = text.lstrip()
        if maybe_json.startswith("{") and maybe_json.endswith("}"):
            try:
                found.update(cls.extract_non_lora_assets_from_json_obj(json.loads(maybe_json)))
            except Exception:
                pass
        patterns = [
            r'(?i)"(ckpt_name|checkpoint_name|checkpoint|model_name|model|unet_name|vae_name|text_encoder|clip_name|clip|vae)"\s*:\s*"([^"]+)"',
            r"(?i)\b(ckpt_name|checkpoint_name|checkpoint|model_name|model|unet_name|vae_name|text_encoder|clip_name|clip|vae)\s*[:=]\s*([^\n\r,]+)",
        ]
        model_keys = {"ckpt_name", "checkpoint_name", "checkpoint", "model_name", "model", "unet_name", "vae_name", "vae", "text_encoder", "clip_name", "clip"}
        for pattern in patterns:
            for key, value in re.findall(pattern, text):
                key_low = str(key).strip().lower()
                cleaned = cls.clean_lora_name(value)
                if not cleaned:
                    continue
                if key_low in model_keys and "lora" not in key_low:
                    found.add(cleaned)
                    continue
                if cls.looks_like_non_lora_asset(cleaned):
                    found.add(cleaned)
                elif any(token in cleaned.lower() for token in ("qwen", "vae", "text_encoder", "clip", "unet")):
                    found.add(cleaned)
        return found

    @classmethod
    def extract_non_lora_assets_from_json_obj(cls, obj: object) -> set[str]:
        found: set[str] = set()
        model_keys = {
            "ckpt_name",
            "checkpoint_name",
            "checkpoint",
            "model_name",
            "model",
            "unet_name",
            "vae_name",
            "vae",
            "text_encoder",
            "clip_name",
            "clip",
            "base_model",
            "diffusion_model",
        }
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_low = str(key).lower()
                if isinstance(value, str):
                    cleaned = cls.clean_lora_name(value)
                    if not cleaned:
                        continue
                    if key_low in model_keys and "lora" not in key_low:
                        found.add(cleaned)
                    elif cls.looks_like_non_lora_asset(cleaned):
                        found.add(cleaned)
                elif isinstance(value, (dict, list)):
                    found.update(cls.extract_non_lora_assets_from_json_obj(value))
        elif isinstance(obj, list):
            for item in obj:
                found.update(cls.extract_non_lora_assets_from_json_obj(item))
        return found

    @staticmethod
    def parse_sd_parameters(text: str) -> tuple[str, str, dict[str, str]]:
        prompt = ""
        negative = ""
        tail_data: dict[str, str] = {}
        if "Negative prompt:" in text:
            prompt_part, rest = text.split("Negative prompt:", 1)
            prompt = prompt_part.strip()
            if "Steps:" in rest:
                neg_part, tail = rest.split("Steps:", 1)
                negative = neg_part.strip()
                tail_data.update(MetadataParser.parse_kv_tail("Steps:" + tail))
            else:
                negative = rest.strip()
        elif "Steps:" in text:
            prompt_part, tail = text.split("Steps:", 1)
            prompt = prompt_part.strip()
            tail_data.update(MetadataParser.parse_kv_tail("Steps:" + tail))
        else:
            prompt = text.strip()
        return prompt, negative, tail_data

    @staticmethod
    def parse_kv_tail(tail: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in tail.split(","):
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key and value:
                out[key] = value
        if "CFG scale" in out and "CFG" not in out:
            out["CFG"] = out["CFG scale"]
        if "Size" in out and "Resolution" not in out:
            out["Resolution"] = out["Size"]
        return out

    @classmethod
    def extract_loras(cls, prompt: str, metadata: dict[str, str]) -> str:
        loras: set[str] = set()
        excluded: set[str] = set()
        loras.update(cls.extract_loras_from_text(prompt))
        excluded.update(cls.extract_non_lora_assets_from_text(prompt))
        for key in ("prompt", "workflow", "extra_pnginfo"):
            value = str(metadata.get(key, "")).strip()
            if value:
                loras.update(cls.extract_loras_from_workflow_json(value))
                excluded.update(cls.extract_model_assets_from_workflow_json(value))
                excluded.update(cls.extract_non_lora_assets_from_text(value))
        for key in ("LoRAs", "loras", "lora", "Lora hashes", "parameters", "Comment", "comment", "UserComment"):
            value = str(metadata.get(key, "")).strip()
            if value:
                loras.update(cls.extract_loras_from_text(value))
                excluded.update(cls.extract_non_lora_assets_from_text(value))
        model_name = cls.clean_lora_name(cls.pick(metadata, ["Model"]))
        if model_name and model_name in loras:
            loras.discard(model_name)
        if model_name:
            excluded.add(model_name)
        if excluded:
            canonical_excluded = {cls.canonical_asset_name(item) for item in excluded if item}
            loras = {item for item in loras if cls.canonical_asset_name(item) not in canonical_excluded}
        loras = {item for item in loras if not cls.looks_like_non_lora_asset(item)}
        return ", ".join(sorted(loras, key=str.lower)) if loras else "None"

    @staticmethod
    def canonical_asset_name(value: str) -> str:
        cleaned = MetadataParser.clean_lora_name(value).lower()
        return re.sub(r"[^a-z0-9]+", "", cleaned) if cleaned else ""

    @staticmethod
    def is_modelish_node_type(node_type: str) -> bool:
        low = (node_type or "").strip().lower()
        if not low or "lora" in low:
            return False
        return any(token in low for token in ("loader", "checkpoint", "ckpt", "unet", "vae", "clip", "textencoder", "text_encoder", "diffusion", "model"))

    @staticmethod
    def iter_strings(value: object) -> list[str]:
        out: list[str] = []
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for sub_value in value.values():
                out.extend(MetadataParser.iter_strings(sub_value))
        elif isinstance(value, list):
            for item in value:
                out.extend(MetadataParser.iter_strings(item))
        return out

    @classmethod
    def extract_model_assets_from_workflow_json(cls, text: str) -> set[str]:
        found: set[str] = set()
        maybe_json = text.lstrip()
        if not text or not (maybe_json.startswith("{") and maybe_json.endswith("}")):
            return found
        try:
            data = json.loads(maybe_json)
        except Exception:
            return found

        def add_candidate(raw_value: str) -> None:
            cleaned = cls.clean_lora_name(raw_value)
            if cleaned:
                found.add(cleaned)

        if isinstance(data, dict) and isinstance(data.get("nodes"), list):
            for node in data["nodes"]:
                if not isinstance(node, dict):
                    continue
                node_type = str(node.get("type", ""))
                if not cls.is_modelish_node_type(node_type):
                    continue
                widgets = node.get("widgets_values")
                if isinstance(widgets, list):
                    for value in cls.iter_strings(widgets):
                        if any(ext in value.lower() for ext in (".safetensors", ".ckpt", ".pth", ".pt")):
                            add_candidate(value)
                props = node.get("properties")
                if isinstance(props, dict):
                    models = props.get("models")
                    if isinstance(models, list):
                        for item in models:
                            if not isinstance(item, dict):
                                continue
                            for key in ("name", "path", "model", "checkpoint", "ckpt_name", "model_name"):
                                value = item.get(key)
                                if isinstance(value, str):
                                    add_candidate(value)

        prompt_obj: object = data["prompt"] if isinstance(data, dict) and isinstance(data.get("prompt"), dict) else data
        if isinstance(prompt_obj, dict):
            for node in prompt_obj.values():
                if not isinstance(node, dict):
                    continue
                class_type = str(node.get("class_type", ""))
                if not cls.is_modelish_node_type(class_type):
                    continue
                inputs = node.get("inputs")
                if not isinstance(inputs, dict):
                    continue
                for key in ("ckpt_name", "checkpoint_name", "checkpoint", "model_name", "model", "unet_name", "vae_name", "vae", "clip_name", "clip", "text_encoder", "text_encoder_name"):
                    value = inputs.get(key)
                    if isinstance(value, str):
                        add_candidate(value)
        return found

    @classmethod
    def extract_loras_from_workflow_json(cls, text: str) -> set[str]:
        found: set[str] = set()
        maybe_json = text.lstrip()
        if not text or not (maybe_json.startswith("{") and maybe_json.endswith("}")):
            return found
        try:
            data = json.loads(maybe_json)
        except Exception:
            return found
        if isinstance(data, dict) and isinstance(data.get("nodes"), list):
            for node in data["nodes"]:
                if isinstance(node, dict) and "lora" in str(node.get("type", "")).lower():
                    found.update(cls.extract_loras_from_lora_node(node))
        prompt_obj: object = data["prompt"] if isinstance(data, dict) and isinstance(data.get("prompt"), dict) else data
        if isinstance(prompt_obj, dict):
            for node in prompt_obj.values():
                if not isinstance(node, dict):
                    continue
                if "lora" not in str(node.get("class_type", "")).lower():
                    continue
                inputs = node.get("inputs", {})
                if isinstance(inputs, dict):
                    found.update(cls.extract_loras_from_lora_payload(inputs))
        return found

    @classmethod
    def extract_loras_from_lora_node(cls, node: dict[str, object]) -> set[str]:
        found: set[str] = set()
        widgets = node.get("widgets_values")
        if isinstance(widgets, list):
            for item in widgets:
                found.update(cls.extract_loras_from_lora_payload(item))
        return found

    @classmethod
    def extract_loras_from_lora_payload(cls, payload: object) -> set[str]:
        found: set[str] = set()
        if isinstance(payload, dict):
            for key, value in payload.items():
                key_low = str(key).lower()
                if isinstance(value, str):
                    if re.fullmatch(r"lora(_\d+)?", key_low) or re.fullmatch(r"lora(_\d+)?_name", key_low) or key_low in {"lora_name", "lora_file", "lora_path", "lora_model_name"}:
                        name = cls.clean_lora_name(value)
                        if name and not cls.looks_like_non_lora_asset(name):
                            found.add(name)
                elif isinstance(value, (dict, list)):
                    found.update(cls.extract_loras_from_lora_payload(value))
        elif isinstance(payload, list):
            for item in payload:
                found.update(cls.extract_loras_from_lora_payload(item))
        return found

    @staticmethod
    def pick(data: dict[str, str], keys: list[str]) -> str:
        for key in keys:
            value = str(data.get(key, "")).strip()
            if value:
                return value
        return ""


class PreviewMixin(UiDispatchMixin):
    def _init_preview_state(self, initial_path: Path | None = None) -> None:
        self.image_path = initial_path
        self.zoom = 1.0
        self.min_zoom = 0.1
        self.max_zoom = 8.0
        self.base_image: Image.Image | None = None
        self._image_pyramid: list[tuple[float, Image.Image]] = []
        self.tk_image: ImageTk.PhotoImage | None = None
        self._image_item: int | None = None
        self._last_render_key: tuple[int, int, int, int, int] | None = None
        self._pending_zoom: float | None = None
        self._wheel_delta_accum = 0
        self._wheel_flush_after_id: str | None = None
        self._quality_after_id: str | None = None
        self._load_token = 0
        self._load_thread: threading.Thread | None = None
        self.path_var = tk.StringVar(value=str(initial_path) if initial_path else "")
        self._loading_item: int | None = None
        self.zoom_var = tk.StringVar(value="100%")
        self._init_ui_dispatcher()

    def _reset_preview_surface(self, message: str) -> None:
        self.base_image = None
        self._image_pyramid = []
        self.tk_image = None
        self._last_render_key = None
        self._pending_zoom = None
        self.zoom = 1.0
        self.zoom_var.set("100%")
        if self._image_item is not None:
            self.canvas.itemconfigure(self._image_item, image="")
        self.canvas.configure(scrollregion=(0, 0, max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())))
        self._show_loading(message)

    def load_path(self, image_path: Path) -> None:
        self.image_path = image_path
        self.path_var.set(str(image_path))
        self._on_preview_path_changed(image_path)
        self._cancel_quality_render()
        self._start_load_image()

    def clear(self, message: str = PREVIEW_EMPTY_TEXT) -> None:
        self.image_path = None
        self.path_var.set("")
        self._on_preview_cleared()
        self._cancel_quality_render()
        self._reset_preview_surface(message)

    def _on_preview_path_changed(self, image_path: Path) -> None:
        return

    def _on_preview_cleared(self) -> None:
        return

    def _on_preview_load_failed(self, exc: Exception) -> None:
        self._show_loading(f"Cannot open preview.\n{exc}")

    def _start_load_image(self) -> None:
        if self.image_path is None:
            return
        self._load_token += 1
        token = self._load_token
        self._reset_preview_surface("Loading preview...")
        self._load_thread = threading.Thread(
            target=self._load_image_worker,
            args=(self.image_path, token),
            daemon=True,
        )
        self._load_thread.start()

    def _load_image_worker(self, image_path: Path, token: int) -> None:
        try:
            with Image.open(image_path) as im:
                base_image = ImageOps.exif_transpose(im).convert("RGB")
            pyramid = self._build_image_pyramid(base_image)
            self._post_to_ui(lambda: self._on_image_loaded(token, image_path, base_image, pyramid))
        except Exception as exc:
            self._post_to_ui(lambda: self._on_image_load_failed(token, exc))

    def _on_image_loaded(self, token: int, image_path: Path, base_image: Image.Image, pyramid: list[tuple[float, Image.Image]]) -> None:
        if token != self._load_token or image_path != self.image_path:
            return
        self.base_image = base_image
        self._image_pyramid = pyramid
        self._hide_loading()
        self.after_idle(self._fit_once_ready)

    def _on_image_load_failed(self, token: int, exc: Exception) -> None:
        if token != self._load_token:
            return
        self._on_preview_load_failed(exc)

    @staticmethod
    def _build_image_pyramid(base_image: Image.Image) -> list[tuple[float, Image.Image]]:
        pyramid: list[tuple[float, Image.Image]] = [(1.0, base_image)]
        current = base_image
        scale = 1.0
        while min(current.width, current.height) > 512:
            next_size = (max(1, current.width // 2), max(1, current.height // 2))
            current = current.resize(next_size, Image.Resampling.BOX)
            scale *= 0.5
            pyramid.append((scale, current))
        return pyramid

    def _choose_render_source(self, target_w: int, target_h: int) -> tuple[float, Image.Image | None]:
        if not self._image_pyramid:
            return 1.0, self.base_image
        source_scale, source_image = self._image_pyramid[0]
        for scale, image in self._image_pyramid[1:]:
            if image.width >= target_w and image.height >= target_h:
                source_scale, source_image = scale, image
                continue
            break
        return source_scale, source_image

    def _show_loading(self, text: str) -> None:
        if self._loading_item is not None:
            self.canvas.itemconfigure(self._loading_item, text=text, state="normal")
            self._position_loading_item()

    def _hide_loading(self) -> None:
        if self._loading_item is not None:
            self.canvas.itemconfigure(self._loading_item, state="hidden")

    def _position_loading_item(self) -> None:
        if self._loading_item is not None:
            self.canvas.coords(self._loading_item, max(20, self.canvas.winfo_width() // 2), max(20, self.canvas.winfo_height() // 2))

    def _on_canvas_configure(self, _event: tk.Event) -> None:
        self._position_loading_item()

    def _fit_once_ready(self) -> None:
        if self.base_image is None:
            return
        self.update_idletasks()
        if self.canvas.winfo_width() <= 2 or self.canvas.winfo_height() <= 2:
            self.after(30, self._fit_once_ready)
            return
        self.fit_to_window()

    def _render(self, resample: int = Image.Resampling.LANCZOS) -> None:
        if self.base_image is None:
            return
        w = max(1, int(self.base_image.width * self.zoom))
        h = max(1, int(self.base_image.height * self.zoom))
        _source_scale, source_image = self._choose_render_source(w, h)
        if source_image is None:
            return
        render_key = (w, h, int(resample), source_image.width, source_image.height)
        if self._last_render_key == render_key and self.tk_image is not None:
            self.zoom_var.set(f"{int(self.zoom * 100)}%")
            return
        resized = source_image.resize((w, h), resample)
        self.tk_image = ImageTk.PhotoImage(resized)
        if self._image_item is None:
            self._image_item = self.canvas.create_image(0, 0, anchor="nw")
        self.canvas.itemconfigure(self._image_item, image=self.tk_image)
        self.canvas.configure(scrollregion=(0, 0, w, h))
        self.zoom_var.set(f"{int(self.zoom * 100)}%")
        self._last_render_key = render_key

    def zoom_in(self) -> None:
        self.set_zoom(self.zoom * 1.15)

    def zoom_out(self) -> None:
        self.set_zoom(self.zoom / 1.15)

    def reset_zoom(self) -> None:
        self.set_zoom(1.0)

    def fit_to_window(self) -> None:
        if self.base_image is None:
            return
        self.update_idletasks()
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        self.set_zoom(min(cw / self.base_image.width, ch / self.base_image.height))
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def set_zoom(self, value: float) -> None:
        self._set_zoom(value, interactive=False)

    def _set_zoom(self, value: float, interactive: bool) -> None:
        clamped = max(self.min_zoom, min(self.max_zoom, value))
        self._pending_zoom = clamped
        if abs(clamped - self.zoom) < 1e-9:
            if self.tk_image is None:
                self._render(Image.Resampling.LANCZOS)
                return
            if interactive:
                self._schedule_quality_render()
            return
        self.zoom = clamped
        if interactive:
            self._render(Image.Resampling.BILINEAR)
            self._schedule_quality_render()
        else:
            self._cancel_quality_render()
            self._render(Image.Resampling.LANCZOS)

    def _schedule_quality_render(self) -> None:
        if self._quality_after_id is not None:
            self.after_cancel(self._quality_after_id)
        self._quality_after_id = self.after(130, self._render_quality)

    def _cancel_quality_render(self) -> None:
        if self._quality_after_id is not None:
            self.after_cancel(self._quality_after_id)
            self._quality_after_id = None

    def _render_quality(self) -> None:
        self._quality_after_id = None
        self._render(Image.Resampling.LANCZOS)

    @staticmethod
    def _wheel_step(event: tk.Event) -> int:
        delta = int(getattr(event, "delta", 0) or 0)
        if delta:
            return delta
        num = int(getattr(event, "num", 0) or 0)
        if num == 4:
            return 120
        if num == 5:
            return -120
        return 0

    def _flush_wheel_zoom(self) -> None:
        self._wheel_flush_after_id = None
        delta = self._wheel_delta_accum
        self._wheel_delta_accum = 0
        if delta == 0:
            return
        ticks = int(delta / 120) or (1 if delta > 0 else -1)
        base = self._pending_zoom if self._pending_zoom is not None else self.zoom
        factor = 1.12 ** abs(ticks)
        target = base * factor if ticks > 0 else base / factor
        self._pending_zoom = target
        self._set_zoom(target, interactive=True)

    def _on_mouse_wheel(self, event: tk.Event) -> None:
        self._wheel_delta_accum += self._wheel_step(event)
        if self._wheel_flush_after_id is None:
            self._wheel_flush_after_id = self.after(24, self._flush_wheel_zoom)

    def _destroy_preview_resources(self) -> None:
        self._load_token += 1
        if self._wheel_flush_after_id is not None:
            try:
                self.after_cancel(self._wheel_flush_after_id)
            except Exception:
                pass
            self._wheel_flush_after_id = None
        self._cancel_quality_render()
        self._shutdown_ui_dispatcher()
