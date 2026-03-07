import unittest
from pathlib import Path

from app_helpers import CatalogHelper, ImageStateHelper, MetadataParser, ReviewHelper, StateStore


TEST_TMP_DIR = Path("tests") / ".tmp"


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_DIR.mkdir(exist_ok=True)

    def test_save_and_load_round_trip(self) -> None:
        state_path = TEST_TMP_DIR / "state_round_trip.json"
        store = StateStore(state_path)
        payload = {
            "selected_dirs": ["E:/images"],
            "ui": {"columns": 4, "thumb_size": 180},
            "images": {"a.png": {"favorite": True, "tags": ["tag"]}},
        }

        save_error = store.save(payload)
        loaded, load_error = store.load()

        self.assertIsNone(save_error)
        self.assertIsNone(load_error)
        self.assertEqual(payload, loaded)

    def test_load_invalid_json_returns_error(self) -> None:
        state_path = TEST_TMP_DIR / "state_invalid.json"
        state_path.write_text("{bad json", encoding="utf-8")
        store = StateStore(state_path)

        loaded, error = store.load()

        self.assertEqual({}, loaded)
        self.assertIsNotNone(error)


class MetadataParserTests(unittest.TestCase):
    def test_parse_sd_parameters(self) -> None:
        prompt, negative, tail = MetadataParser.parse_sd_parameters(
            "cat on table\nNegative prompt: blur\nSteps: 20, CFG scale: 7, Size: 1024x1024"
        )

        self.assertEqual("cat on table", prompt)
        self.assertEqual("blur", negative)
        self.assertEqual("20", tail["Steps"])
        self.assertEqual("7", tail["CFG"])
        self.assertEqual("1024x1024", tail["Resolution"])

    def test_extract_generation_fields_from_comfy_prompt(self) -> None:
        raw = {
            "prompt": (
                '{"1":{"class_type":"CLIPTextEncode","inputs":{"text":"sunset"}},"2":{"class_type":"CLIPTextEncode","inputs":{"text":"low quality"}},'
                '"3":{"class_type":"KSampler","inputs":{"sampler_name":"euler","scheduler":"normal","seed":123,"cfg":6.5,"steps":28}},'
                '"4":{"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":"model.safetensors"}}}'
            )
        }

        parsed = MetadataParser.extract_generation_fields(raw)

        self.assertEqual("sunset", parsed["Prompt"])
        self.assertEqual("low quality", parsed["Negative prompt"])
        self.assertEqual("euler", parsed["Sampler"])
        self.assertEqual("model.safetensors", parsed["Model"])

    def test_extract_loras_filters_model_assets(self) -> None:
        metadata = {
            "Model": "flux_dev.safetensors",
            "prompt": '{"prompt":{"1":{"class_type":"LoraLoader","inputs":{"lora_name":"detailer.safetensors","model":"flux_dev.safetensors"}}}}',
        }

        loras = MetadataParser.extract_loras("portrait <lora:detailer:1>", metadata)

        self.assertEqual("detailer", loras)


class ImageStateHelperTests(unittest.TestCase):
    def test_prune_image_state_removes_empty_entries_and_normalizes_tags(self) -> None:
        image_state = {
            "a.png": {"favorite": False, "tags": [" Test ", "test"], "review_status": ""},
            "b.png": {"favorite": False, "tags": [], "review_status": ""},
            "c.png": {"favorite": False, "tags": [], "review_status": "reject"},
        }

        pruned = ImageStateHelper.prune_image_state(image_state)

        self.assertEqual(
            {
                "a.png": {"favorite": False, "tags": ["test"]},
                "c.png": {"favorite": False, "tags": [], "review_status": "reject"},
            },
            pruned,
        )

    def test_normalized_review_status(self) -> None:
        self.assertEqual("reject", ImageStateHelper.normalized_review_status("reject"))
        self.assertEqual("unreviewed", ImageStateHelper.normalized_review_status("keep"))


class CatalogHelperTests(unittest.TestCase):
    def test_build_search_record(self) -> None:
        prompt_text, search_text = CatalogHelper.build_search_record(
            Path("image.png"),
            {"Prompt": "Sunset", "Model": "flux", "LoRAs": "detailer"},
            ["portrait"],
        )

        self.assertEqual("sunset", prompt_text)
        self.assertIn("image.png", search_text)
        self.assertIn("portrait", search_text)
        self.assertIn("flux", search_text)

    def test_best_root_for_path(self) -> None:
        path = Path("E:/images/set1/cat/image.png")
        roots = [Path("E:/images"), Path("E:/images/set1")]

        best = CatalogHelper.best_root_for_path(path, roots)

        self.assertEqual(Path("E:/images/set1"), best)

    def test_scan_image_roots(self) -> None:
        scan_root = TEST_TMP_DIR / "scan_root"
        nested = scan_root / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        (scan_root / "one.png").write_bytes(b"x")
        (nested / "two.jpg").write_bytes(b"x")
        (nested / "note.txt").write_text("skip", encoding="utf-8")

        found, root_by_key, mtime_by_key, cancelled = CatalogHelper.scan_image_roots(
            [scan_root],
            {".png", ".jpg"},
        )

        self.assertFalse(cancelled)
        self.assertEqual(2, len(found))
        self.assertTrue(all(str(path) in root_by_key for path in found))
        self.assertTrue(all(str(path) in mtime_by_key for path in found))

    def test_filter_paths(self) -> None:
        paths = [Path("a.png"), Path("b.png")]
        states = {
            "a.png": {"favorite": True, "review_status": "unreviewed"},
            "b.png": {"favorite": False, "review_status": "reject"},
        }
        search = {
            "a.png": ("sunset", "a.png | sunset | portrait"),
            "b.png": ("city", "b.png | city | night"),
        }

        filtered, cancelled = CatalogHelper.filter_paths(
            paths,
            query="sunset",
            tag_filter="",
            favorites_only=False,
            review_filter="All",
            sort_mode="Newest",
            get_state=lambda path: states[path.name],
            get_review_status=lambda path: str(states[path.name]["review_status"]),
            get_search_record=lambda path: search[path.name],
            sort_key=lambda path: 1 if path.name == "a.png" else 0,
        )

        self.assertFalse(cancelled)
        self.assertEqual([Path("a.png")], filtered)


class ReviewHelperTests(unittest.TestCase):
    def test_next_review_focus_path(self) -> None:
        paths = [Path("a.png"), Path("b.png"), Path("c.png")]

        self.assertEqual(Path("c.png"), ReviewHelper.next_review_focus_path(Path("b.png"), paths))
        self.assertEqual(Path("b.png"), ReviewHelper.next_review_focus_path(Path("c.png"), paths))

    def test_next_path_after_deletion(self) -> None:
        paths = [Path("a.png"), Path("b.png"), Path("c.png")]

        next_path = ReviewHelper.next_path_after_deletion(paths, Path("b.png"), {"b.png"})

        self.assertEqual(Path("c.png"), next_path)


if __name__ == "__main__":
    unittest.main()
