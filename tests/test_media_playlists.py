import copy
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from config_store import ConfigStore, DEFAULT_CONFIG, validate_config
import display_controller
from display_controller import (
    DisplayController,
    playlist_item_config,
    playlist_module_config,
    scheduled_playlist_items,
)
from media_resolver import candidate_media_references


class MediaPlaylistTests(unittest.TestCase):
    def test_playlist_item_config_randomly_selects_from_checked_media(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        item = copy.deepcopy(config["playlist"][0])
        item["sources"] = ["first.gif", "second.gif", "third.gif"]
        choices = []

        def choose(options):
            choices.append(tuple(options))
            return options[1]

        selected = playlist_item_config(config, item, chooser=choose)

        self.assertEqual(choices, [("first.gif", "second.gif", "third.gif")])
        self.assertEqual(selected["media"]["splash"], "second.gif")
        self.assertEqual(config["media"]["splash"], "built-in/rubik_squares.gif")

    def test_playlist_module_config_randomly_selects_first_matching_rows_pool(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        splash = next(item for item in config["playlist"] if item["module"] == "splash")
        choices = []

        selected = playlist_module_config(
            config, "splash",
            chooser=lambda options: choices.append(tuple(options)) or options[-1],
        )

        self.assertEqual(choices, [tuple(splash["sources"])])
        self.assertEqual(selected["media"]["splash"], splash["sources"][-1])

    def test_device_draft_randomly_selects_and_uses_a_pinned_pool_member(self):
        controller = DisplayController.__new__(DisplayController)
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        splash = next(item for item in candidate["playlist"] if item["module"] == "splash")
        selected_source = splash["sources"][1]
        selected_handle = object()
        request = Mock(
            config={"module": "splash", "candidate": candidate},
            timeout_seconds=5,
            pinned_media={source: object() for source in splash["sources"]},
        )
        request.pinned_media[selected_source] = selected_handle
        module = Mock()
        module.run.return_value = True

        with (
            patch("display_controller.random.choice", return_value=selected_source),
            patch("display_controller.importlib.import_module", return_value=module),
        ):
            rendered = controller.render_device_draft(
                Mock(), request, stop_event=threading.Event(),
            )

        self.assertTrue(rendered)
        called_candidate = module.run.call_args.args[1]
        self.assertEqual(called_candidate["media"]["splash"], selected_source)
        self.assertIs(module.run.call_args.kwargs["pinned_handle"], selected_handle)

    def test_scheduled_media_execution_keeps_selected_source_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            media_root.mkdir()
            path = media_root / "selected.gif"
            path.write_bytes(b"reviewed-bytes")
            config = copy.deepcopy(DEFAULT_CONFIG)
            splash = next(item for item in config["playlist"] if item["module"] == "splash")
            splash["source"] = "selected.gif"
            splash["sources"] = ["selected.gif"]
            config["media"]["splash"] = "selected.gif"
            store = Mock(media_root=media_root)
            controller = DisplayController(Mock(), store)
            module = Mock()

            def run(_matrix, _config, **options):
                replacement = media_root / "replacement.gif"
                replacement.write_bytes(b"replacement")
                replacement.replace(path)
                with options["pinned_handle"].dup_file() as handle:
                    self.assertEqual(handle.read(), b"reviewed-bytes")
                return True

            module.run.side_effect = run
            with patch("display_controller.importlib.import_module", return_value=module):
                self.assertTrue(controller._execute("splash", config, 1))

    def test_saved_media_preview_snapshots_and_pins_at_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            media_root.mkdir()
            first = media_root / "first.gif"
            second = media_root / "second.gif"
            first.write_bytes(b"first-reviewed")
            second.write_bytes(b"second-reviewed")
            config = copy.deepcopy(DEFAULT_CONFIG)
            splash = next(item for item in config["playlist"] if item["module"] == "splash")
            splash["source"] = "first.gif"
            splash["sources"] = ["first.gif", "second.gif"]
            config["media"]["splash"] = "first.gif"
            store = Mock(media_root=media_root)
            store.get.return_value = config
            controller = DisplayController(Mock(), store)

            with (
                patch.object(controller, "start"),
                patch("display_controller.random.choice", return_value="first.gif") as choose,
            ):
                controller.preview("splash")
            queued = controller._take_preview()
            self.assertEqual(choose.call_count, 1)
            replacement = media_root / "replacement.gif"
            replacement.write_bytes(b"replacement")
            replacement.replace(first)
            config["media"]["splash"] = "second.gif"
            module = Mock()

            def run(_matrix, selected, **options):
                self.assertEqual(selected["media"]["splash"], "first.gif")
                with options["pinned_handle"].dup_file() as handle:
                    self.assertEqual(handle.read(), b"first-reviewed")
                return True

            module.run.side_effect = run
            try:
                with patch("display_controller.importlib.import_module", return_value=module):
                    self.assertTrue(controller._execute(
                        queued.module_name,
                        queued.config,
                        1,
                        pinned_handle=queued.pinned_handle,
                    ))
            finally:
                queued.close()

    def test_pending_saved_media_preview_closes_pin_when_superseded_or_stopped(self):
        for action in ("supersede", "stop"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                media_root = Path(directory) / "media"
                media_root.mkdir()
                (media_root / "selected.gif").write_bytes(b"reviewed")
                (media_root / "other.gif").write_bytes(b"replacement-preview")
                config = copy.deepcopy(DEFAULT_CONFIG)
                splash = next(
                    item for item in config["playlist"] if item["module"] == "splash"
                )
                splash["source"] = "selected.gif"
                splash["sources"] = ["selected.gif"]
                config["media"]["splash"] = "selected.gif"
                store = Mock(media_root=media_root)
                store.get.return_value = config
                controller = DisplayController(Mock(), store)
                with patch.object(controller, "start"):
                    controller.preview("splash")
                queued = controller._preview_name

                if action == "supersede":
                    with patch.object(controller, "start"):
                        controller.preview_media("other.gif")
                    self.assertTrue(controller.stop())
                else:
                    controller.stop()

                self.assertTrue(queued.pinned_handle.closed)

    def test_saved_custom_media_preview_executes_admitted_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            media_root.mkdir()
            source = media_root / "basic.gif"
            source.write_bytes(b"saved-preview-original")
            custom = {
                "id": "custom_basic_media", "name": "Basic media", "type": "media",
                "source": source.name, "fit": "contain", "x": 0, "y": 0,
                "width": 64, "height": 32, "loops": 1,
                "playback_speed_percent": 100.0,
            }
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["custom_modules"] = [custom]
            store = Mock(media_root=media_root, get=Mock(return_value=config))
            controller = DisplayController(Mock(), store)

            with patch.object(controller, "start"):
                controller.preview(custom["id"])
            pending = controller._take_preview()
            replacement = media_root / "replacement.gif"
            replacement.write_bytes(b"saved-preview-replacement")
            os.replace(replacement, source)
            module = Mock()
            module.run.return_value = True

            try:
                with patch(
                    "display_controller.importlib.import_module", return_value=module,
                ):
                    rendered = controller._execute(
                        pending.module_name, pending.config, 1,
                        pinned_handles=pending.pinned_handles,
                    )
                self.assertTrue(rendered)
                pinned = module.run.call_args.kwargs["pinned_handles"][source.name]
                with pinned.dup_file() as handle:
                    self.assertEqual(handle.read(), b"saved-preview-original")
            finally:
                pending.close()
            self.assertTrue(pinned.closed)

    def test_saved_media_free_preview_owner_closes_without_resolver(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        store = Mock()
        store.get.return_value = config
        controller = DisplayController(Mock(), store)
        controller.start = Mock(return_value=None)

        controller.preview("weather")
        queued = controller._take_preview()

        self.assertIsInstance(queued, display_controller._PinnedModulePreview)
        queued.close()

    def test_scheduled_custom_media_execution_pins_invocation_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            media_root = Path(directory) / "media"
            media_root.mkdir()
            source = media_root / "scheduled.gif"
            source.write_bytes(b"scheduled-original")
            custom = {
                "id": "custom_scheduled_media", "name": "Scheduled media",
                "type": "media", "source": source.name, "fit": "contain",
                "x": 0, "y": 0, "width": 64, "height": 32, "loops": 1,
                "playback_speed_percent": 100.0,
            }
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["custom_modules"] = [custom]
            controller = DisplayController(Mock(), Mock(media_root=media_root))
            module = Mock()
            observed = {}

            def run(_matrix, _config, **options):
                replacement = media_root / "replacement.gif"
                replacement.write_bytes(b"scheduled-replacement")
                os.replace(replacement, source)
                pinned = options["pinned_handles"][source.name]
                observed["pinned"] = pinned
                with pinned.dup_file() as handle:
                    self.assertEqual(handle.read(), b"scheduled-original")
                return True

            module.run.side_effect = run
            with patch(
                "display_controller.importlib.import_module", return_value=module,
            ):
                self.assertTrue(controller._execute(custom["id"], config, 1))

            self.assertTrue(observed["pinned"].closed)

    def test_saved_media_preview_after_stop_begins_is_rejected_before_pinning(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        store = Mock(media_root=Path("/unused"))
        store.get.return_value = config
        controller = DisplayController(Mock(), store)
        stop_reached_after_drain = threading.Event()
        release_stop = threading.Event()
        stop_results = []
        resolver = Mock()
        resolver.resolve.return_value = object()

        def block_after_initial_preview_drain():
            stop_reached_after_drain.set()
            self.assertTrue(release_stop.wait(1))

        with (
            patch.object(
                controller.device_preview_manager,
                "close",
                side_effect=block_after_initial_preview_drain,
            ),
            patch(
                "display_controller.MediaResolver", return_value=resolver,
            ) as resolver_factory,
        ):
            stopper = threading.Thread(
                target=lambda: stop_results.append(controller.stop(timeout=1))
            )
            stopper.start()
            self.assertTrue(stop_reached_after_drain.wait(0.2))
            admission_error = None
            try:
                controller.preview("splash")
            except RuntimeError as error:
                admission_error = error
            finally:
                release_stop.set()
                stopper.join(1)

        self.assertFalse(stopper.is_alive())
        self.assertEqual(stop_results, [True])
        self.assertIsInstance(admission_error, RuntimeError)
        self.assertIn("stopping", str(admission_error))
        self.assertIsNone(controller._preview_name)
        resolver_factory.assert_not_called()

    def test_preview_start_failure_rolls_back_queue_and_closes_new_pin(self):
        controller = DisplayController(Mock(), Mock())
        displaced_resolver = Mock()
        new_resolver = Mock()
        displaced = display_controller._PinnedModulePreview(
            "splash", {}, displaced_resolver, Mock(),
        )
        pending = display_controller._PinnedModulePreview(
            "sleep", {}, new_resolver, Mock(),
        )
        prior_announcement = {"id": "announcement_prior"}
        controller._preview_name = displaced
        controller._media_preview_name = "prior.gif"
        controller._announcement_preview = prior_announcement

        with (
            patch.object(controller, "start", side_effect=RuntimeError("start failed")),
            self.assertRaisesRegex(RuntimeError, "start failed"),
        ):
            controller._queue_preview(pending=pending)

        self.assertIs(controller._preview_name, displaced)
        self.assertEqual(controller._media_preview_name, "prior.gif")
        self.assertEqual(controller._announcement_preview, prior_announcement)
        new_resolver.close.assert_called_once_with()
        displaced_resolver.close.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_playlist_preview_switches_mode_without_superseding_its_first_render(self):
        dashboard = (
            Path(__file__).parents[1] / "app" / "templates" / "dashboard.html"
        ).read_text()
        start = dashboard.index("function previewMediaPlaylistItem")
        end = dashboard.index("\nfunction ", start + 1)
        function_source = dashboard[start:end]
        script = r"""
let previewMediaItemId=null,previewMode='live',previewModule=null;
let starts=0,schedules=0;
const row={dataset:{itemId:'splash_default',module:'splash'}};
const button={closest(){return{querySelector(){return row}}}};
function selectEditPreviewModule(name,schedule){previewModule=name;if(schedule!==false)throw new Error('playlist preview selected with implicit schedule')}
function requestPreviewMode(mode){previewMode=mode;starts+=1}
function scheduleEditPreview(delay){if(delay!==0)throw new Error('wrong delay');schedules+=1}
FUNCTION_SOURCE
previewMediaPlaylistItem(button);
if(previewMediaItemId!=='splash_default'||previewModule!=='splash'||previewMode!=='edit'||starts!==1||schedules!==0)throw new Error(JSON.stringify({previewMediaItemId,previewModule,previewMode,starts,schedules}));
previewMediaPlaylistItem(button);
if(starts!==1||schedules!==1)throw new Error(JSON.stringify({starts,schedules}));
console.log('MEDIA_PLAYLIST_PREVIEW_PASS');
""".replace("FUNCTION_SOURCE", function_source)

        node = shutil.which("node")
        assert node is not None
        completed = subprocess.run(
            [node, "-e", script], text=True, capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("MEDIA_PLAYLIST_PREVIEW_PASS", completed.stdout)

    def test_dashboard_exposes_accessible_media_playlist_actions(self):
        dashboard = (
            Path(__file__).parents[1] / "app" / "templates" / "dashboard.html"
        ).read_text()

        self.assertIn("legend.textContent='Select '+module+' screens'", dashboard)
        self.assertIn("Splash screens play in the active playlist", dashboard)
        self.assertIn("Sleep screens rotate outside active hours", dashboard)
        self.assertIn("data-media-item-id", dashboard)
        self.assertIn('data-media-source="{{ file.name }}"', dashboard)
        self.assertIn("item.sources=mediaPlaylistSources(row)", dashboard)
        self.assertNotIn("Add splash screen", dashboard)
        self.assertNotIn("Add sleep screen", dashboard)
        self.assertNotIn('id="splash-file"', dashboard)
        self.assertNotIn('id="sleep-file"', dashboard)

    def test_default_config_has_four_splash_choices_and_editable_media_rows(self):
        config = validate_config(copy.deepcopy(DEFAULT_CONFIG), enforce_save_bounds=True)

        media_rows = [
            item for item in config["playlist"]
            if item["module"] in {"splash", "sleep"}
        ]
        self.assertEqual(
            [(item["module"], item["source"]) for item in media_rows],
            [
                ("splash", "built-in/rubik_squares.gif"),
                ("sleep", "built-in/sleep.gif"),
            ],
        )
        for item in media_rows:
            self.assertRegex(item["id"], rf"^{item['module']}_[a-z0-9_]+$")
            self.assertEqual(
                set(item),
                {"id", "module", "source", "sources", "duration", "enabled", "layout"},
            )
            self.assertEqual(item["layout"]["width"], 64)
            self.assertEqual(item["layout"]["height"], 32)
        self.assertEqual(media_rows[0]["sources"], [
            "built-in/rubik_squares.gif",
            "built-in/waterfall_squares.gif",
            "built-in/loading_squares.gif",
            "built-in/zigzag_squares.gif",
        ])

    def test_multiple_media_rows_are_validated_and_all_sources_are_pinned(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        second = copy.deepcopy(config["playlist"][0])
        second.update({
            "id": "splash_second", "source": "second.gif",
            "sources": ["second.gif"],
        })
        config["playlist"].insert(1, second)

        validated = validate_config(config, enforce_save_bounds=True)

        self.assertEqual(
            [item["source"] for item in validated["playlist"] if item["module"] == "splash"],
            ["built-in/rubik_squares.gif", "second.gif"],
        )
        self.assertIn("second.gif", candidate_media_references(validated))

    def test_checked_media_pool_is_validated_and_every_source_is_pinned(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        splash = next(item for item in config["playlist"] if item["module"] == "splash")
        splash["sources"] = [
            "built-in/rubik_squares.gif", "first.gif", "second.png",
        ]

        validated = validate_config(config, enforce_save_bounds=True)

        selected = next(
            item for item in validated["playlist"] if item["module"] == "splash"
        )
        self.assertEqual(selected["sources"], splash["sources"])
        self.assertTrue(set(splash["sources"]).issubset(candidate_media_references(validated)))

        for invalid in ([], ["first.gif", "first.gif"], ["../escape.gif"]):
            with self.subTest(invalid=invalid):
                candidate = copy.deepcopy(DEFAULT_CONFIG)
                row = next(
                    item for item in candidate["playlist"]
                    if item["module"] == "splash"
                )
                row["sources"] = invalid
                with self.assertRaises(ValueError):
                    validate_config(candidate, enforce_save_bounds=True)

    def test_playlist_item_config_selects_each_rows_media_and_layout(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        item = copy.deepcopy(config["playlist"][0])
        item["source"] = "alternate.gif"
        item["sources"] = ["alternate.gif"]
        item["layout"]["x"] = 7

        selected = playlist_item_config(config, item)

        self.assertEqual(selected["media"]["splash"], "alternate.gif")
        self.assertEqual(selected["media_layout"]["splash"]["x"], 7)
        self.assertEqual(config["media"]["splash"], "built-in/rubik_squares.gif")

    def test_active_and_inactive_schedules_use_their_own_ordered_rows(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        second_splash = copy.deepcopy(config["playlist"][0])
        second_splash.update({
            "id": "splash_second", "source": "second.gif",
            "sources": ["second.gif"],
        })
        second_sleep = copy.deepcopy(config["playlist"][-1])
        second_sleep.update({
            "id": "sleep_second", "source": "night.gif",
            "sources": ["night.gif"],
        })
        config["playlist"].insert(1, second_splash)
        config["playlist"].append(second_sleep)

        active = scheduled_playlist_items(config, active=True)
        inactive = scheduled_playlist_items(config, active=False)

        self.assertEqual(
            [item.get("source", item["module"]) for item in active[:3]],
            ["built-in/rubik_squares.gif", "second.gif", "weather"],
        )
        self.assertNotIn("sleep", [item["module"] for item in active])
        self.assertEqual(
            [item["source"] for item in inactive],
            ["built-in/sleep.gif", "night.gif"],
        )

    def test_schema_three_single_slots_migrate_to_editable_playlist_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            legacy = copy.deepcopy(DEFAULT_CONFIG)
            legacy["schema_version"] = 3
            legacy["media"]["splash"] = "legacy.gif"
            legacy["playlist"] = [
                {"module": item["module"], "duration": item["duration"], "enabled": item["enabled"]}
                for item in legacy["playlist"]
                if item["module"] != "sleep"
            ]
            path.write_text(json.dumps(legacy))

            store = ConfigStore(path, media_dir=Path(temporary) / "media")
            migrated = store.get()

        rows = [item for item in migrated["playlist"] if item["module"] in {"splash", "sleep"}]
        self.assertEqual(migrated["schema_version"], 4)
        self.assertEqual(rows[0]["source"], "legacy.gif")
        self.assertEqual(rows[1]["source"], "built-in/sleep.gif")
        self.assertEqual(migrated["revision"], 1)

    def test_playlist_only_update_persists_multiple_rows_and_refreshes_legacy_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(
                Path(temporary) / "config.json",
                media_dir=Path(temporary) / "media",
            )
            playlist = copy.deepcopy(store.get()["playlist"])
            first = next(item for item in playlist if item["module"] == "splash")
            first["source"] = "first.gif"
            first["sources"] = ["first.gif"]
            second = copy.deepcopy(first)
            second.update({
                "id": "splash_second", "source": "second.gif",
                "sources": ["second.gif"],
            })
            playlist.insert(1, second)

            saved = store.update({"playlist": playlist})
            reloaded = ConfigStore(
                store.path, media_dir=Path(temporary) / "media",
            ).get()

        self.assertEqual(saved["media"]["splash"], "first.gif")
        self.assertEqual(
            [item["source"] for item in reloaded["playlist"] if item["module"] == "splash"],
            ["first.gif", "second.gif"],
        )

    def test_disabling_every_sleep_row_does_not_synthesize_a_sleep_screen(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        for item in config["playlist"]:
            if item["module"] == "sleep":
                item["enabled"] = False

        self.assertEqual(scheduled_playlist_items(config, active=False), [])

    def test_empty_sleep_playlist_idles_instead_of_busy_spinning(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        for item in config["playlist"]:
            if item["module"] == "sleep":
                item["enabled"] = False
        controller = DisplayController(Mock(), Mock())
        reads = 0

        def get_config():
            nonlocal reads
            reads += 1
            if reads == 2:
                controller._shutdown.set()
            return config

        controller.config_store.get = get_config
        controller._is_active = Mock(return_value=False)
        original_wait = controller._shutdown.wait
        waits = []

        def wait(timeout):
            waits.append(timeout)
            return original_wait(0)

        controller._shutdown.wait = wait
        controller._run_loop_unchecked()

        self.assertIn(0.25, waits)

    def test_sleep_playlist_stops_before_starting_another_row_at_active_boundary(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        first = next(item for item in config["playlist"] if item["module"] == "sleep")
        first["duration"] = 60
        second = copy.deepcopy(first)
        second.update({
            "id": "sleep_second", "source": "second.gif",
            "sources": ["second.gif"],
        })
        config["playlist"].append(second)
        store = Mock(get=Mock(return_value=config))
        controller = DisplayController(Mock(), store)
        active_checks = iter((False, False, True))
        checked_times = []
        boundary_times = []

        def is_active(_config, now=None):
            checked_times.append(now)
            active = next(active_checks)
            if active:
                controller._shutdown.set()
            return active

        def seconds_until_change(_config, now=None):
            boundary_times.append(now)
            return 5

        controller._is_active = is_active
        controller._seconds_until_schedule_change = seconds_until_change
        controller._take_preview = Mock(return_value=None)
        controller._take_media_preview = Mock(return_value=None)
        controller._take_announcement_preview = Mock(return_value=None)
        controller.device_preview_manager.take_pending = Mock(return_value=None)
        calls = []
        controller._execute = lambda name, selected, duration: calls.append(
            (name, selected["media"]["sleep"], duration)
        )

        controller._run_loop_unchecked()

        self.assertEqual(calls, [("sleep", "built-in/sleep.gif", 5)])
        self.assertIsNotNone(checked_times[1])
        self.assertIs(boundary_times[0], checked_times[1])

    def test_active_playlist_stops_before_starting_another_row_at_inactive_boundary(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        first = next(item for item in config["playlist"] if item["module"] == "splash")
        first["duration"] = 60
        second = next(item for item in config["playlist"] if item["module"] == "weather")
        config["playlist"] = [first, second]
        store = Mock(get=Mock(return_value=config))
        controller = DisplayController(Mock(), store)
        active_checks = iter((True, True, False))
        checked_times = []
        boundary_times = []

        def is_active(config, now=None):
            del config
            checked_times.append(now)
            active = next(active_checks)
            if not active:
                controller._shutdown.set()
            return active

        def seconds_until_change(config, now=None):
            del config
            boundary_times.append(now)
            return 5

        controller._is_active = is_active
        controller._seconds_until_schedule_change = seconds_until_change
        controller._take_preview = Mock(return_value=None)
        controller._take_media_preview = Mock(return_value=None)
        controller._take_announcement_preview = Mock(return_value=None)
        controller.device_preview_manager = Mock(take_pending=Mock(return_value=None))
        calls = []

        def execute(name, config, duration):
            del config
            calls.append((name, duration))

        controller._execute = execute

        controller._run_loop_unchecked()

        self.assertEqual(calls, [("splash", 5)])
        self.assertIsNotNone(checked_times[1])
        self.assertIs(boundary_times[0], checked_times[1])

    def test_contract_three_playlist_save_preserves_v4_media_row_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(
                Path(temporary) / "config.json",
                media_dir=Path(temporary) / "media",
            )
            playlist = copy.deepcopy(store.get()["playlist"])
            first = next(item for item in playlist if item["module"] == "splash")
            second = copy.deepcopy(first)
            second.update({
                "id": "splash_second",
                "source": "special.gif",
                "sources": ["special.gif"],
                "layout": {**second["layout"], "x": 9},
            })
            playlist.insert(1, second)
            current = store.update({"playlist": playlist})
            reduced = [
                {key: item[key] for key in ("module", "duration", "enabled")}
                if item["module"] in {"splash", "sleep"} else copy.deepcopy(item)
                for item in current["playlist"]
            ]

            saved = store.update({"playlist": reduced}, client_contract=3)

        splash_rows = [item for item in saved["playlist"] if item["module"] == "splash"]
        self.assertEqual([item["id"] for item in splash_rows], ["splash_default", "splash_second"])
        self.assertEqual([item["source"] for item in splash_rows], [
            "built-in/rubik_squares.gif", "special.gif",
        ])
        self.assertEqual([item["layout"]["x"] for item in splash_rows], [0, 9])

    def test_contract_three_playlist_save_cannot_remove_unrepresented_v4_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(
                Path(temporary) / "config.json",
                media_dir=Path(temporary) / "media",
            )
            playlist = copy.deepcopy(store.get()["playlist"])
            first = next(item for item in playlist if item["module"] == "splash")
            second = copy.deepcopy(first)
            second.update({
                "id": "splash_second", "source": "special.gif",
                "sources": ["special.gif"],
            })
            playlist.insert(1, second)
            current = store.update({"playlist": playlist})
            reduced = []
            seen_splash = False
            for item in current["playlist"]:
                if item["module"] == "splash":
                    if seen_splash:
                        continue
                    seen_splash = True
                    reduced.append({
                        key: item[key] for key in ("module", "duration", "enabled")
                    })
                else:
                    reduced.append(copy.deepcopy(item))

            saved = store.update({"playlist": reduced}, client_contract=3)

        self.assertEqual(
            [item["source"] for item in saved["playlist"] if item["module"] == "splash"],
            ["built-in/rubik_squares.gif", "special.gif"],
        )

    def test_contract_three_save_preserves_unrepresented_v4_row_position(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(
                Path(temporary) / "config.json",
                media_dir=Path(temporary) / "media",
            )
            playlist = copy.deepcopy(store.get()["playlist"])
            first = next(item for item in playlist if item["module"] == "splash")
            second = copy.deepcopy(first)
            second.update({
                "id": "splash_second", "source": "special.gif",
                "sources": ["special.gif"],
            })
            playlist.append(second)
            current = store.update({"playlist": playlist})
            expected_order = [item.get("id", item["module"]) for item in current["playlist"]]

            reduced = []
            seen_splash = False
            for item in current["playlist"]:
                if item["module"] == "splash":
                    if seen_splash:
                        continue
                    seen_splash = True
                    reduced.append({
                        key: item[key] for key in ("module", "duration", "enabled")
                    })
                else:
                    reduced.append(copy.deepcopy(item))

            saved = store.update({"playlist": reduced}, client_contract=3)

        self.assertEqual(
            [item.get("id", item["module"]) for item in saved["playlist"]],
            expected_order,
        )

    def test_contract_three_save_preserves_entirely_omitted_v4_media_module(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(
                Path(temporary) / "config.json",
                media_dir=Path(temporary) / "media",
            )
            playlist = copy.deepcopy(store.get()["playlist"])
            first = next(item for item in playlist if item["module"] == "splash")
            second = copy.deepcopy(first)
            second.update({
                "id": "splash_second", "source": "special.gif",
                "sources": ["special.gif"],
            })
            playlist.append(second)
            current = store.update({"playlist": playlist})
            expected_order = [item.get("id", item["module"]) for item in current["playlist"]]
            reduced = []
            seen_splash = False
            for item in current["playlist"]:
                if item["module"] == "sleep":
                    continue
                if item["module"] == "splash":
                    if seen_splash:
                        continue
                    seen_splash = True
                    reduced.append({
                        key: item[key] for key in ("module", "duration", "enabled")
                    })
                else:
                    reduced.append(copy.deepcopy(item))

            saved = store.update({"playlist": reduced}, client_contract=3)

        self.assertEqual(
            [item.get("id", item["module"]) for item in saved["playlist"]],
            expected_order,
        )

    def test_contract_three_scalar_write_cannot_mutate_v4_pool_or_canonical_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(
                Path(temporary) / "config.json",
                media_dir=Path(temporary) / "media",
            )
            saved = store.update(
                {"media": {"splash": "built-in/loading_squares.gif"}},
                client_contract=3,
            )

        splash = next(item for item in saved["playlist"] if item["module"] == "splash")
        self.assertEqual(saved["media"]["splash"], "built-in/rubik_squares.gif")
        self.assertEqual(splash["source"], "built-in/rubik_squares.gif")
        self.assertEqual(splash["sources"], [
            "built-in/rubik_squares.gif",
            "built-in/waterfall_squares.gif",
            "built-in/loading_squares.gif",
            "built-in/zigzag_squares.gif",
        ])

    def test_contract_three_rich_row_cannot_mutate_v4_identity_pool_or_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            current = store.get()
            splash = next(item for item in current["playlist"] if item["module"] == "splash")
            submitted = copy.deepcopy(current["playlist"])
            row = next(item for item in submitted if item["module"] == "splash")
            row.update({
                "id": "legacybad",
                "source": "built-in/zigzag_squares.gif",
                "sources": ["built-in/zigzag_squares.gif"],
                "layout": {**row["layout"], "x": 13},
                "duration": row["duration"] + 1,
                "enabled": not row["enabled"],
            })

            saved = store.update({"playlist": submitted}, client_contract=3)

        saved_splash = next(item for item in saved["playlist"] if item["module"] == "splash")
        for field in ("id", "source", "sources", "layout"):
            self.assertEqual(saved_splash[field], splash[field])
        self.assertEqual(saved_splash["duration"], splash["duration"] + 1)
        self.assertEqual(saved_splash["enabled"], (not splash["enabled"]))

    def test_only_contracts_three_and_four_are_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            with self.assertRaisesRegex(ValueError, "client contract is invalid"):
                store.validate_candidate({}, client_contract=5)

    def test_schema_three_migration_synthesizes_each_missing_required_media_row(self):
        for missing in ({"splash"}, {"sleep"}, {"splash", "sleep"}):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                legacy = copy.deepcopy(DEFAULT_CONFIG)
                legacy["schema_version"] = 3
                legacy["playlist"] = [
                    item for item in legacy["playlist"] if item["module"] not in missing
                ]
                path.write_text(json.dumps(legacy))

                migrated = ConfigStore(path).get()

                modules = [item["module"] for item in migrated["playlist"]]
                self.assertEqual(modules.count("splash"), 1)
                self.assertEqual(modules.count("sleep"), 1)

    def test_contract_four_rejects_removing_all_splash_or_sleep_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            for module_name in ("splash", "sleep"):
                with self.subTest(module=module_name):
                    candidate = [
                        item for item in store.get()["playlist"]
                        if item["module"] != module_name
                    ]

                    with self.assertRaisesRegex(
                        ValueError,
                        f"playlist must contain at least one {module_name} row",
                    ):
                        store.update({"playlist": candidate}, client_contract=4)

    def test_contract_four_rejects_source_outside_checked_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            playlist = store.get()["playlist"]
            splash = next(item for item in playlist if item["module"] == "splash")
            splash["source"] = "built-in/zigzag_squares.gif"
            splash["sources"] = ["built-in/waterfall_squares.gif"]

            with self.assertRaisesRegex(
                ValueError, "playlist media source must be the first checked source",
            ):
                store.update({"playlist": playlist}, client_contract=4)

    def test_malformed_playlist_item_uses_controlled_validation_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(
                Path(temporary) / "config.json",
                media_dir=Path(temporary) / "media",
            )

            with self.assertRaises(ValueError):
                store.validate_candidate(
                    {"playlist": [None]}, canonicalize_media=False,
                )


if __name__ == "__main__":
    unittest.main()
