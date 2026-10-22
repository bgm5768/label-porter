import json
import sys
import tempfile
import traceback
from pathlib import Path

if len(sys.argv) == 3 and sys.argv[1] == '--apply-update':
    from label_porter.update_install import helper_main
    raise SystemExit(helper_main(sys.argv[2]))

from label_porter.gui import App, main


def self_test(output):
    """Exercise the frozen Tk/resources/engine without opening a user window."""
    try:
        import tkinter as tk
        from unittest.mock import patch
        from label_porter.engine import run
        from label_porter.rules import expression
        from label_porter.label_counts import LabelCounts
        from label_porter.widgets import create_root
        for digits, expected in ((3, 3.329), (1, 3.3), (0, 3.0)):
            assert expression({'literal': 3.329999,
                               'transforms': [{'op': 'truncate', 'digits': digits}]}, {}) == expected
        label_counts = LabelCounts({'rules': [{'kind': 'field', 'from': '/labels', 'to': '/labels', 'type': 'array'}]})
        label_counts.add('output_before', {'labels': [1]})
        label_counts.add('output', {'labels': [1, 2, 3]})
        assert label_counts.rows()[0]['delta'] == 2
        root = create_root()
        root.withdraw()
        with patch.object(App, "_load_settings"):
            app = App(root)
        app._save_settings = lambda: None  # Self-tests must never alter the user's working project.
        root.update_idletasks()
        app._show_updates(); root.update_idletasks()
        assert app.page_title.get() == '프로그램 업데이트'
        assert not app.updater.busy  # Self-tests never start network checks.
        app._back()
        if getattr(sys, 'frozen', False) and sys.platform == 'win32':
            import os
            from label_porter.update_install import _parent_handle
            kernel, handle = _parent_handle(os.getpid(), Path(sys.executable).resolve())
            try: assert kernel.WaitForSingleObject(handle, 0) == 258
            finally: kernel.CloseHandle(handle)
        rule_path, config = next((path, rule_config) for path, rule_config in app.bundled_rules.values() if path.stem == "generic")
        assert len(app.bundled_rules) == 4
        assert len(app.tabs.tabs()) == 4
        assert len(app.steps.tabs()) == 4
        assert [b['text'] for b in app.source_panel.buttons] == [b['text'] for b in app.result_panel.buttons]
        assert str(app.steps.tab(1, "state")) == "disabled"
        assert "AI 규칙 작성 가이드" in [app.tabs.tab(tab, "text") for tab in app.tabs.tabs()]
        assert "schema_version" in app.ai_guide_text
        assert app.ai_guide_box.source == app.ai_guide_text
        assert app.help_box.source == app.help_text
        assert app.capabilities_box.source == app.capabilities_text
        assert app.architecture_box.source == app.architecture_text
        assert app.tabs.tab(3, "text") == "아키텍처 · 규칙 엔진"
        assert len(app.architecture_box.diagrams) == 3
        assert not app.architecture_box.diagrams_loaded
        app.architecture_box._load_diagrams(); root.update()
        assert len(app.architecture_box.diagram_widgets) == 3
        assert all(label._diagram_image.width() >= 80 for label in app.architecture_box.diagram_widgets.values())
        app.architecture_box.diagram_scale.set('150%'); app.architecture_box._scale_diagrams()
        app.architecture_box.diagram_scale.set('화면에 맞춤'); app.architecture_box._scale_diagrams()
        assert 'project_rule.json' in app.architecture_text and 'rule.json' in app.architecture_text
        assert "<table>" in app.ai_guide_box.html_source
        assert app.ai_guide_box.browser.document.getElementById("section-0").textContent
        assert "rebuild_items" in app.capabilities_text
        assert app.tabs.tab(2, "text") == "후처리 지원 범위"
        assert all(text not in app.help_text for text in ("one_before", "one_after", "008", "013"))
        assert not hasattr(app, "assets_directory")
        with patch('label_porter.gui.filedialog.askopenfilename', return_value=str(rule_path)):
            app._load_rule()
        assert app.current_step == 0 and app.rule_ready
        inspector = app.rule_inspector
        assert inspector.preview is None
        assert [inspector.pages.tab(tab, "text") for tab in inspector.pages.tabs()] == ["필드 연결", "규칙 JSON"]
        assert json.loads(inspector.raw_box.get("1.0", "end")) == config
        app.primary_button.invoke()
        assert app.current_step == 1 and app.rule_ready
        assert not hasattr(app, 'editor') and not hasattr(app, '_new_project')
        with tempfile.TemporaryDirectory(prefix="label_porter_selftest_") as temp:
            base = Path(temp)
            source = base / "source"
            source.mkdir()
            original = json.dumps({"id": "test", "label": "old", "metadata": {"keep": True}})
            (source / "one.json").write_text(original, encoding="utf-8")
            (base / "result.json").write_text(json.dumps({"source_id": "test", "corrected_label": "new"}), encoding="utf-8")
            app.source.set(str(source)); app.result_paths = [str(base / "result.json")]
            app.output.set(str(base / "session_saved")); app._render_results()
            app._wizard()
            import time
            deadline = time.monotonic() + 10
            while app.running and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
            editor = app.mapping
            assert editor.rule_config == config
            assert [editor.pages.tab(tab, "text") for tab in editor.pages.tabs()] == ["필드 연결", "필드 연결 프리뷰", "규칙 JSON"]
            assert all(str(box["state"]) == "disabled" for box in editor.preview.json_boxes)
            assert editor.raw_box["state"] == "disabled"
            assert not editor.canvas.bind("<B1-Motion>")
            assert not editor.canvas.bind("<Delete>")
            import time
            deadline = time.monotonic() + 10
            while editor.preview.pair is None and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
            assert editor.preview.pair['key'] == 'test', editor.preview.status.get()
            assert editor.preview.configured
            app._show_tools(1); root.update()
            assert app.route == 'help' and not hasattr(app, 'help_window')
            app.back_button.invoke(); root.update()
            assert app.mapping is editor and editor.preview.pair['key'] == 'test'
            app.primary_button.invoke()
            root.update()
            assert app.current_step == 3 and app.connection_confirmed
            cached = editor.preview.cache
            app._navigate(1); app._navigate(2); root.update()
            assert app.mapping is editor and editor.preview.cache is cached
            assert len(editor.preview.table.get_children()) == 1
            assert [editor.preview.views.tab(tab, "text") for tab in editor.preview.views.tabs()] == [
                "전체 데이터 목록", "실제 값 연결", "JSON 한 쌍 · 처리 후"]
            app.primary_button.invoke()
            assert app._session_ready() and app.primary_button['text'] == '전체 데이터 검증'
            assert not (base/'session_saved').exists()
            app.primary_button.invoke()
            deadline = time.monotonic() + 15
            while app.running and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
            assert app.preview_fingerprint and app.last_summary['preview']
            assert (app.last_run / 'reports/report.xlsx').is_file()
            assert not (app.last_run/'data').exists()
            assert app.primary_button['text'] == '복사본 저장'
            app.primary_button.invoke()
            deadline = time.monotonic() + 15
            while app.running and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
            assert app._saved(), app.status.get()
            assert (app.last_run / 'reports/report.xlsx').is_file()
            assert str(app.excel_button['state']) == 'normal'
            assert json.loads((app.last_run / 'data/one.json').read_text(encoding='utf-8')) == editor.preview.pair['output']
            from label_porter.mapping import load_samples, preview_rule, import_rule, compile_rule
            for _, rule_config in app.bundled_rules.values():
                restored = import_rule(rule_config)
                assert compile_rule(restored, None, None, rule_config["source"].get("records_path", ""), rule_config["result"].get("records_path", ""), rule_config["name"]) == rule_config
            first_preview = run(source, [base / "result.json"], base / "output", config, preview=True)
            second_preview = run(source, [base / "result.json"], base / "output", config, preview=True)
            assert first_preview.directory == second_preview.directory == base / "output" / "미리보기"
            result = run(source, [base / "result.json"], base / "output", config)
            assert result.summary["status"] == "completed", result.summary
            assert (source / "one.json").read_text(encoding="utf-8") == original
            actual = json.loads((result.directory / "data" / "one.json").read_text(encoding="utf-8"))
            assert actual == {"id": "test", "label": "new", "metadata": {"keep": True}}
            assert json.loads((result.directory / "changed" / "labels" / "one.json").read_text(encoding="utf-8")) == actual
            assert result.summary["partition_records"] == {"changed": 1, "unchanged": 0, "examine": 0}
            assert result.directory.name == "001" and len(result.directory.parent.name) == 10
            assert "assets" not in result.summary
            excluded_config = {**config, 'include_when': [{'left': '/source_id', 'op': 'ne', 'value': 'test'}]}
            excluded_run = run(source, [base / 'result.json'], base / 'excluded_output', excluded_config)
            assert (excluded_run.directory / 'examine/excluded/원본/one.json').read_bytes() == (source / 'one.json').read_bytes()
            assert (excluded_run.directory / 'examine/excluded/보안/result.json').read_bytes() == (base / 'result.json').read_bytes()
            many = base / "many_originals"
            many.mkdir()
            for name in ("a", "b"):
                (many / (name + ".json")).write_text(json.dumps({"label": "old"}), encoding="utf-8")
            many_result = base / "many_result.json"
            many_result.write_text(json.dumps([{"file": "b.json", "new": "B"}, {"file": "a.json", "new": "A"}]), encoding="utf-8")
            collection, exports = load_samples(many), load_samples(many_result)
            file_rule = {"schema_version": 1, "name": "many files", "source": {"key": {"meta": "file_name"}},
                            "result": {"key": "/file"}, "rules": [{"kind": "field", "from": "/new", "to": "/label"}]}
            graph = import_rule(file_rule, collection, exports)
            check = preview_rule(file_rule, graph.source_rows, graph.result_rows, collection.meta)
            assert collection.file_count == 2 and check["counts"] == {"updated": 2}, check
            assert check["all_source_key_counts"] == {"matched": 2}
            from label_porter.json_viewer import JsonViewer
            from label_porter.formats import Document
            viewer = app.json_viewer = JsonViewer(root, on_close=app._json_viewer_closed)
            viewer.window.withdraw()
            viewer.display(Document([[{"items": [1, 2, 3]}]], "self-test"))
            assert "최상위 항목 1개" in viewer.status.get()
            viewer.window.destroy()
            assert app.json_viewer is None and not viewer.values and viewer.events.empty()
            viewer = JsonViewer(root, paths=[many])
            viewer.window.withdraw()
            deadline = time.monotonic() + 10
            while (len(viewer.browser.paths) != 2 or not viewer.values or viewer.loading) and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
            assert len(viewer.browser.paths) == 2 and viewer.values and not viewer.loading
            viewer.browser.query.set('b.json'); viewer.browser.filter()
            assert len(viewer.browser.filtered) == 1
            import gc, weakref
            viewer_ref = weakref.ref(viewer)
            viewer.window.destroy()
            assert viewer.browser.on_select is None and not viewer.browser.paths
            del viewer
            gc.collect()
            assert viewer_ref() is None
            chosen = run([many/'a.json'], [many_result], base/'selected_output', file_rule)
            assert chosen.summary['source_records'] == 1 and chosen.summary['status'] != 'failed'
            assert (chosen.directory/'data/a.json').is_file() and not (chosen.directory/'data/b.json').exists()
            from label_porter.rules import apply_record
            from label_porter.config import validate_config
            rebuild_rule = validate_config({
                "schema_version": 1, "name": "rebuild check",
                "source": {"key": "/id"}, "result": {"key": "/id"},
                "rules": [{"kind": "rebuild_items", "from": "/items", "to": "/items",
                           "source_key": {"scope": "source", "path": "/id"}, "result_key": "/id",
                           "rules": [{"kind": "field", "from": {"meta": "item_number"}, "to": "/id", "allow_create": True},
                                     {"kind": "field", "from": "/text", "to": "/text", "allow_create": True}]}]})
            rebuilt, _, _, state = apply_record({"items": [{"id": 10, "text": "old"}]},
                                                {"items": [{"id": 99, "text": "new"}, {"id": 100, "text": "added"}]},
                                                rebuild_rule, {})
            assert state == "updated" and rebuilt["items"] == [{"id": 1, "text": "new"}, {"id": 2, "text": "added"}]
            from label_porter.pair_preview import load_preview, match_preview
            preview_source, preview_result = base / 'rebuild_source.json', base / 'rebuild_result.json'
            preview_source.write_text(json.dumps({'id': 1, 'items': [{'id': 10, 'text': 'old'}]}), encoding='utf-8')
            preview_result.write_text(json.dumps({'id': 1, 'items': [{'id': 10, 'text': 'new'}]}), encoding='utf-8')
            rebuild_pair = match_preview(load_preview(rebuild_rule, preview_source, [preview_result]))
            old_text = next(row for row in rebuild_pair['rows'] if row.get('target_path') == '/items/0/text')
            assert old_text['source'] == '"old"' and old_text['comparison'] == 'id'
            assert rebuild_pair['output']['items'] == [{'id': 1, 'text': 'new'}]
            # The builder reuses its in-memory preview and inputs.
            easy_result = base / "easy.json"
            easy_result.write_text(json.dumps([{"dataID": "external", "importData_id": "test",
                                               "label_error_modify": {"data": [{"value": "easy"}]}}]), encoding="utf-8")
            app._show_step(0); app._show_builder()
            builder = app.builder
            builder.source.set(str(source)); builder.results = [str(easy_result)]; builder._render_results()
            builder.next()
            deadline = time.monotonic() + 10
            while builder.data is None and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
            assert builder.data is not None, builder.status.get()
            assert builder.selected == {"/label"}
            builder.next()
            deadline = time.monotonic() + 10
            while builder.preview.pair is None and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
            assert builder.preview.pair is not None
            remembered = builder.preview.cache
            builder.next()
            deadline = time.monotonic() + 10
            while (app.mapping is None or app.mapping.preview.pair is None) and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
            assert app.mapping.preview.cache is remembered and app._session_ready()
            assert app._get_config()["source"]["key"] == "/id"
            assert app._get_config()["result"]["key"] == "/importData_id"
            assert app.mapping.preview.pair["output"]["label"] == "easy"
            app._close_mapping(); app._close_builder()
        root.destroy()
        Path(output).write_text(json.dumps({"ok": True, "preview_only_stage3": True, "full_validation_stage4": True, "mermaid_diagrams": 3, "mermaid_offline": True, "tk": True, "rules": 4, "engine": True, "original_preserved": True, "split_output": True, "date_sequence": True, "reusable_preview": True, "media_feature_removed": True, "ai_guide_bundled": True, "general_project_help": True, "inline_help": True, "shared_navigation": True, "four_step_workflow": True, "automatic_data_preview": True, "ai_rule_roundtrip": True, "multiple_sample_files": True, "per_file_identity": True, "ai_first_workflow": True, "readonly_field_review": True, "inline_rule_inspection": True, "explicit_next_step": True, "easy_rules": True}), encoding="utf-8")
        return 0
    except Exception:
        Path(output).write_text(json.dumps({"ok": False, "error": traceback.format_exc()}), encoding="utf-8")
        return 1

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test(sys.argv[2]))
    main()
