import json
import sys
import tempfile
import traceback
from pathlib import Path

from label_porter.gui import App, main


def self_test(output):
    """Exercise the frozen Tk/resources/engine without opening a user window."""
    try:
        import tkinter as tk
        from unittest.mock import patch
        from label_porter.engine import run
        root = tk.Tk()
        root.withdraw()
        with patch.object(App, "_load_settings"):
            app = App(root)
        app._save_settings = lambda: None  # Self-tests must never alter the user's working project.
        root.update_idletasks()
        config = next(profile for path, profile in app.profiles.values() if path.stem == "generic")
        assert len(app.profiles) == 4
        assert len(app.tabs.tabs()) == 3
        assert len(app.steps.tabs()) == 3
        assert str(app.steps.tab(1, "state")) == "disabled"
        assert "AI 규칙 작성" in [app.tabs.tab(tab, "text") for tab in app.tabs.tabs()]
        assert "schema_version" in app.ai_guide_text
        assert app.ai_guide_box.get("1.0", "end-1c") == app.ai_guide_text
        assert app.help_box.get("1.0", "end-1c") == app.help_text
        assert all(text not in app.help_text for text in ("one_before", "one_after", "008", "013"))
        assert not hasattr(app, "assets_directory")
        # Loaded rules can be confirmed without requiring sample files or a separate save.
        app._set_config(config)
        app._wizard()
        editor = app.mapping
        assert editor.profile == config
        assert "필드 연결" in [editor.pages.tab(tab, "text") for tab in editor.pages.tabs()]
        assert editor.raw_box["state"] == "disabled"
        assert not editor.canvas.bind("<B1-Motion>")
        assert not editor.canvas.bind("<Delete>")
        root.update()
        editor.apply_button.invoke()
        root.update()
        assert app.current_step == 1 and app.rule_ready
        assert app.steps.index(app.steps.select()) == 1
        with tempfile.TemporaryDirectory(prefix="label_porter_selftest_") as temp:
            base = Path(temp)
            source = base / "source"
            source.mkdir()
            original = json.dumps({"id": "test", "label": "old", "metadata": {"keep": True}})
            (source / "one.json").write_text(original, encoding="utf-8")
            (base / "result.json").write_text(json.dumps({"source_id": "test", "corrected_label": "new"}), encoding="utf-8")
            from label_porter.mapping import load_samples, preview_profile, import_profile, compile_profile
            for _, profile in app.profiles.values():
                restored = import_profile(profile)
                assert compile_profile(restored, None, None, profile["source"].get("records_path", ""), profile["result"].get("records_path", ""), profile["name"]) == profile
            first_preview = run(source, [base / "result.json"], base / "output", config, preview=True)
            second_preview = run(source, [base / "result.json"], base / "output", config, preview=True)
            assert first_preview.directory == second_preview.directory == base / "output" / "미리보기"
            result = run(source, [base / "result.json"], base / "output", config)
            assert result.summary["status"] == "completed", result.summary
            assert (source / "one.json").read_text(encoding="utf-8") == original
            actual = json.loads((result.directory / "data" / "one.json").read_text(encoding="utf-8"))
            assert actual == {"id": "test", "label": "new", "metadata": {"keep": True}}
            assert json.loads((result.directory / "changed" / "labels" / "one.json").read_text(encoding="utf-8")) == actual
            assert result.summary["partition_records"] == {"changed": 1, "unchanged": 0}
            assert result.directory.name == "001" and len(result.directory.parent.name) == 10
            assert "assets" not in result.summary
            many = base / "many_originals"
            many.mkdir()
            for name in ("a", "b"):
                (many / (name + ".json")).write_text(json.dumps({"label": "old"}), encoding="utf-8")
            many_result = base / "many_result.json"
            many_result.write_text(json.dumps([{"file": "b.json", "new": "B"}, {"file": "a.json", "new": "A"}]), encoding="utf-8")
            collection, exports = load_samples(many), load_samples(many_result)
            file_profile = {"schema_version": 1, "name": "many files", "source": {"key": {"meta": "file_name"}},
                            "result": {"key": "/file"}, "rules": [{"kind": "field", "from": "/new", "to": "/label"}]}
            graph = import_profile(file_profile, collection, exports)
            check = preview_profile(file_profile, graph.source_rows, graph.result_rows, collection.meta)
            assert collection.file_count == 2 and check["counts"] == {"updated": 2}, check
            assert check["all_source_key_counts"] == {"matched": 2}
            from label_porter.json_viewer import JsonViewer
            from label_porter.formats import Document
            viewer = JsonViewer(root)
            viewer.window.withdraw()
            viewer.display(Document([[{"items": [1, 2, 3]}]], "self-test"))
            assert "최상위 항목 1개" in viewer.status.get()
            viewer.window.destroy()
            from label_porter.rules import apply_record
            from label_porter.config import validate_config
            rebuild_profile = validate_config({
                "schema_version": 1, "name": "rebuild check",
                "source": {"key": "/id"}, "result": {"key": "/id"},
                "rules": [{"kind": "rebuild_items", "from": "/items", "to": "/items",
                           "source_key": {"scope": "source", "path": "/id"}, "result_key": "/id",
                           "rules": [{"kind": "field", "from": {"meta": "item_number"}, "to": "/id", "allow_create": True},
                                     {"kind": "field", "from": "/text", "to": "/text", "allow_create": True}]}]})
            rebuilt, _, _, state = apply_record({"items": [{"id": 10, "text": "old"}]},
                                                {"items": [{"id": 99, "text": "new"}, {"id": 100, "text": "added"}]},
                                                rebuild_profile, {})
            assert state == "updated" and rebuilt["items"] == [{"id": 1, "text": "new"}, {"id": 2, "text": "added"}]
        root.destroy()
        Path(output).write_text(json.dumps({"ok": True, "tk": True, "profiles": 4, "engine": True, "original_preserved": True, "split_output": True, "date_sequence": True, "reusable_preview": True, "media_feature_removed": True, "ai_guide_bundled": True, "general_project_help": True, "three_step_workflow": True, "ai_rule_roundtrip": True, "multiple_sample_files": True, "per_file_identity": True, "ai_first_workflow": True, "readonly_field_review": True, "automatic_next_step": True}), encoding="utf-8")
        return 0
    except Exception:
        Path(output).write_text(json.dumps({"ok": False, "error": traceback.format_exc()}), encoding="utf-8")
        return 1

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test(sys.argv[2]))
    main()
