"""图像清洗的误合并及跨人物筛选回归，不需要联网或模型。"""
import importlib.util
from pathlib import Path

from PIL import Image

spec = importlib.util.spec_from_file_location("image_pilot", Path(__file__).parents[1] / "image_pilot.py")
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def test_joint_attributes_cannot_cross_people():
    def person(hat, sleeve):
        return {"attributes": {"hat": {"label": hat}, "sleeve": {"label": sleeve}}}
    rows = [
        {"filename": "different_people.jpg", "persons": [person("hat", "long_sleeves"), person("no_hat", "short_sleeves")]},
        {"filename": "same_person.jpg", "persons": [person("hat", "short_sleeves")]},
        {"filename": "occluded.jpg", "persons": [person("unclear", "short_sleeves")]},
    ]
    assert pilot.select_images(rows, {"hat": "hat", "sleeve": "short_sleeves"}) == ["same_person.jpg"]


def test_near_duplicate_groups_do_not_merge_transitively(tmp_path, monkeypatch):
    # A≈B and B≈C does not imply A≈C. Always compare to retained representatives.
    monkeypatch.setattr(pilot, "WORK", tmp_path)
    inputs = []
    for name, color in [("a.png", 20), ("b.png", 40), ("c.png", 60)]:
        Image.new("RGB", (40, 40), (color, 10, 10)).save(tmp_path / name)
        inputs.append({"filename": name, "relative_path": name})
    def stats(a, b):
        return (2, .01, 0) if {a.name, b.name} in [{"a.png", "b.png"}, {"b.png", "c.png"}] else (20, .2, 0)
    monkeypatch.setattr(pilot, "image_pair_stats", stats)
    kept, pairs = pilot.deduplicate(inputs)
    assert [x["filename"] for x in kept] == ["a.png", "c.png"]
    assert [(x["filename"], x["keep"]) for x in pairs] == [("b.png", "a.png")]
    assert all((tmp_path / r["filename"]).exists() for r in inputs)


def test_same_shape_different_colors_are_not_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "WORK", tmp_path)
    rows = []
    for name, color in [("red.png", "red"), ("blue.png", "blue")]:
        Image.new("RGB", (40, 40), color).save(tmp_path / name)
        rows.append({"filename": name, "relative_path": name})
    kept, pairs = pilot.deduplicate(rows)
    assert len(kept) == 2 and not pairs
