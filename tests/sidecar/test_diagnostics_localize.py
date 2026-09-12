import json

from internal.diagnostics import localize as module


def _report():
    return {
        "v2": True,
        "summary": "warn",
        "checks": [
            {
                "id": "firewall",
                "ok": False,
                "detail": "Wrong port (got none, needs 8765,8080)",
                "detail_key": "diag.firewall.fail.detail",
                "detail_params": {"ports": "8765,8080"},
                "guidance": "Allow both ports.",
                "guidance_key": "diag.firewall.fail.guidance",
                "guidance_params": {"ports": "8765,8080"},
            },
            {"id": "permissions", "ok": True, "detail": "raw only"},
        ],
        "groups": {
            "system": {
                "label_key": "diag.v2.group.system",
                "items": [
                    {
                        "id": "app_version",
                        "status": "ok",
                        "detail": "Version 1.0.0",
                        "detail_key": "diag.v2.item.app_version.detail",
                        "detail_params": {"version": "1.0.0"},
                        "hint": None,
                    },
                    {
                        "id": "data_dir",
                        "status": "warn",
                        "detail": "/data (not writable)",
                        "hint": "raw hint",
                        "hint_key": "diag.v2.item.data_dir.warn.hint",
                    },
                ],
            }
        },
    }


def test_translate_replaces_every_placeholder_and_misses_resolve_to_none():
    table = {"k": "{a} and {a} then {b}"}
    assert module.translate(table, "k", {"a": 1, "b": 2}) == "1 and 1 then 2"
    assert module.translate(table, "missing") is None
    assert module.translate(table, None) is None
    assert module.translate(table, "k", {"a": "x"}) == "x and x then {b}"


def test_translations_fall_back_to_english_and_cache(tmp_path, monkeypatch):
    (tmp_path / "en.json").write_text(json.dumps({"k": "english"}), encoding="utf-8")
    monkeypatch.setattr(module, "locales_dir", lambda: str(tmp_path))
    monkeypatch.setattr(module, "_cache", {})
    assert module.translations("fr") == {"k": "english"}
    assert module.translations("") == {"k": "english"}
    assert module.translations("fr") is module.translations("fr")


def test_translations_of_a_missing_directory_are_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "locales_dir", lambda: str(tmp_path / "absent"))
    monkeypatch.setattr(module, "_cache", {})
    assert module.translations("zh-CN") == {}


def test_localize_adds_resolved_text_and_leaves_the_report_untouched(tmp_path, monkeypatch):
    (tmp_path / "zh-CN.json").write_text(
        json.dumps(
            {
                "diag.firewall.fail.detail": "端口 {ports} 未放行",
                "diag.firewall.fail.guidance": "请放行 {ports}",
                "diag.v2.group.system": "系统",
                "diag.v2.item.app_version": "应用版本",
                "diag.v2.item.app_version.detail": "版本 {version}",
                "diag.v2.item.data_dir.warn.hint": "数据目录不可写",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "locales_dir", lambda: str(tmp_path))
    monkeypatch.setattr(module, "_cache", {})
    original = _report()
    report = module.localize(original, "zh-CN")

    check, raw = report["checks"][0], original["checks"][0]
    assert check["detail_text"] == "端口 8765,8080 未放行"
    assert check["guidance_text"] == "请放行 8765,8080"
    assert "detail_text" not in raw and raw["detail_key"] == "diag.firewall.fail.detail"
    # A check with no resolvable key keeps only its raw detail.
    assert "detail_text" not in report["checks"][1]

    group = report["groups"]["system"]
    assert group["label_text"] == "系统"
    assert group["items"][0]["label_text"] == "应用版本"
    assert group["items"][0]["detail_text"] == "版本 1.0.0"
    assert group["items"][0].get("hint_text") is None
    assert group["items"][1]["hint_text"] == "数据目录不可写"
    assert original["groups"]["system"]["items"][0].get("label_text") is None


def test_localize_returns_the_report_when_no_locale_can_be_read(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "locales_dir", lambda: str(tmp_path / "absent"))
    monkeypatch.setattr(module, "_cache", {})
    original = _report()
    assert module.localize(original, "zh-CN") is original


def test_real_locales_cover_every_group_and_item_label():
    """The shipped locale files must label every group and v2 item the report
    can emit — a new item without a label would render as a bare id."""
    table = module.translations("zh-CN")
    assert table, "the zh-CN locale file must be present"
    for _group_id, key in module.GROUP_LABEL_KEYS:
        assert key in table
    for item_id, key in module.ITEM_LABEL_KEYS.items():
        assert key in table, item_id
