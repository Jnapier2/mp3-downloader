from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

import mp3_downloader as app


class PublicSafetyTests(unittest.TestCase):
    def test_public_identity_is_simple_and_generic(self):
        self.assertEqual(app.APP_VERSION, "1.0.0")
        self.assertEqual(app.RELEASE_CHANNEL, "public")
        self.assertEqual(app.EXPORTS_DIR.name, "support_exports")
        self.assertEqual(
            app.EXPECTED_RUNTIME_PINS,
            {"certifi": "2026.6.17", "yt-dlp": "2026.7.4"},
        )
        self.assertEqual(app.exact_pinned_requirements(), app.EXPECTED_RUNTIME_PINS)

    def test_url_guard_rejects_unsafe_inputs_without_dns(self):
        accepted = app.validate_public_url(
            "https://example.org/authorized-media",
            allow_private_networks=False,
            resolve_dns=False,
        )
        self.assertEqual(accepted, "https://example.org/authorized-media")

        rejected = (
            "file:///tmp/media",
            "https://user:secret@example.org/media",
            "http://127.0.0.1/media",
            "http://[::1]/media",
            "http://localhost/media",
            "https://service.local/media",
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(app.DownloaderError):
                    app.validate_public_url(value, allow_private_networks=False, resolve_dns=False)

    def test_dns_results_are_checked_without_network_access(self):
        public_records = [(None, None, None, None, ("8.8.8.8", 443))]
        private_records = [(None, None, None, None, ("10.0.0.5", 443))]

        with mock.patch.object(app.socket, "getaddrinfo", return_value=public_records):
            self.assertEqual(
                app.validate_public_url("https://example.org/media", resolve_dns=True),
                "https://example.org/media",
            )
        with mock.patch.object(app.socket, "getaddrinfo", return_value=private_records):
            with self.assertRaises(app.DownloaderError):
                app.validate_public_url("https://example.org/media", resolve_dns=True)

    def test_config_clamps_limits_and_contains_output_template(self):
        config = app.validate_config(
            {
                "config_version": 1,
                "mp3_quality_kbps": 999,
                "max_size_mb": -1,
                "allow_private_networks": "false",
                "output_filename_template": "../escape/%(id)s.%(ext)s",
                "rights_holder": "untrusted override",
            }
        )
        self.assertEqual(config["mp3_quality_kbps"], 320)
        self.assertEqual(config["max_size_mb"], 10.0)
        self.assertFalse(config["allow_private_networks"])
        self.assertEqual(config["output_filename_template"], app.DEFAULT_CONFIG["output_filename_template"])
        self.assertEqual(config["rights_holder"], "Gateway Information Group LLC")

    def test_output_filesystem_guard_rejects_escape_and_multiple_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out_dir = root / "downloads"
            out_dir.mkdir()
            output = out_dir / "track.mp3"
            output.write_bytes(b"synthetic audio fixture")
            self.assertEqual(
                app.validate_output_filesystem_state(output, out_dir),
                output.resolve(),
            )

            outside = root / "outside.mp3"
            outside.write_bytes(b"outside fixture")
            with self.assertRaises(app.DownloaderError):
                app.validate_output_filesystem_state(outside, out_dir)

            linked = out_dir / "linked.mp3"
            try:
                app.os.link(output, linked)
            except (NotImplementedError, OSError):
                self.skipTest("Hard links are unavailable on this filesystem")
            with self.assertRaises(app.DownloaderError):
                app.validate_output_filesystem_state(output, out_dir)

    def test_audio_policy_rejects_drm_private_and_live_sources(self):
        safe = {"availability": "public", "acodec": "mp3", "is_live": False}
        app.ensure_audio_candidate_info(safe, {"allow_live_streams": False})

        unsafe_items = (
            {"has_drm": True, "acodec": "mp3"},
            {"availability": "subscriber_only", "acodec": "mp3"},
            {"availability": "public", "is_live": True, "acodec": "mp3"},
            {"availability": "public", "acodec": "none", "formats": []},
        )
        for info in unsafe_items:
            with self.subTest(info=info):
                with self.assertRaises(app.DownloaderError):
                    app.ensure_audio_candidate_info(info, {"allow_live_streams": False})

    def test_missing_and_unlisted_availability_are_not_presented_as_public(self):
        for availability, expected_label in ((None, "unknown"), ("unlisted", "unlisted")):
            info = {
                "id": "synthetic-id",
                "title": "Synthetic Audio",
                "acodec": "mp3",
                "is_live": False,
            }
            if availability is not None:
                info["availability"] = availability
            output = io.StringIO()
            with (
                mock.patch.object(
                    app,
                    "validate_public_url",
                    return_value="https://example.org/authorized-media",
                ),
                mock.patch.object(app, "extract_audio_info", return_value=info),
                mock.patch.object(app, "append_run_history"),
                redirect_stdout(output),
            ):
                path, duplicate = app.download_one(
                    url="https://example.org/authorized-media",
                    out_dir=Path("downloads"),
                    config={"allow_private_networks": False},
                    quality_kbps=192,
                    overwrite=False,
                    dry_run=False,
                    list_only=True,
                    queue_worker=False,
                    queue_job_id="",
                )
            text = output.getvalue()
            self.assertIsNone(path)
            self.assertFalse(duplicate)
            self.assertIn(
                "authorization and publication status are not verified",
                text,
            )
            self.assertIn(f"Reported availability: {expected_label}", text)
            self.assertNotIn("Confirmed public", text)

    def test_redaction_removes_urls_email_tokens_and_known_paths(self):
        email = "owner" + "@" + "example.org"
        raw = (
            f"open https://example.org/private?id=7; {email}; "
            f"Authorization: Bearer abc.def.ghi; token=secret-value; {Path.home()}\\private"
        )
        redacted = app.redact_sensitive_text(raw)
        self.assertNotIn("https://example.org/private", redacted)
        self.assertNotIn(email, redacted)
        self.assertNotIn("abc.def.ghi", redacted)
        self.assertNotIn("secret-value", redacted)
        self.assertNotIn(str(Path.home()), redacted)
        self.assertIn("URL_REDACTED", redacted)

    def test_support_export_is_small_redacted_and_offline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "allow_private_networks": True,
                        "ffmpeg_location": str(Path("C:/") / "Users" / "Example" / "private-tools"),
                        "user_agent": "Support test https://example.org/private?id=9",
                    }
                ),
                encoding="utf-8",
            )

            replacements = {
                "DOWNLOADS_DIR": root / "downloads",
                "LOGS_DIR": root / "logs",
                "STATE_DIR": root / "state",
                "TEMP_DIR": root / "temp",
                "EXPORTS_DIR": root / "support_exports",
                "RUN_HISTORY_PATH": root / "state" / "run_history.jsonl",
                "QUEUE_STATE_PATH": root / "state" / "link_queue_status.json",
            }
            original = {name: getattr(app, name) for name in replacements}
            try:
                for name, value in replacements.items():
                    setattr(app, name, value)
                (root / "logs").mkdir(parents=True)
                (root / "state").mkdir(parents=True)
                sensitive_markers = (
                    "unlisted-media-7",
                    "Hidden Test Title",
                    "Uploader Alias",
                    "private-source.example",
                    r"D:\Archive\Sensitive\song.mp3",
                )
                (root / "logs" / "mp3_downloader.log").write_text(
                    " | ".join(sensitive_markers), encoding="utf-8"
                )
                replacements["RUN_HISTORY_PATH"].write_text(
                    json.dumps(
                        {
                            "media_id": sensitive_markers[0],
                            "title": sensitive_markers[1],
                            "uploader": sensitive_markers[2],
                            "source_host": sensitive_markers[3],
                            "output": sensitive_markers[4],
                        }
                    ),
                    encoding="utf-8",
                )
                replacements["QUEUE_STATE_PATH"].write_text(
                    json.dumps({"jobs": [{"title": sensitive_markers[1]}]}),
                    encoding="utf-8",
                )
                with (
                    mock.patch.object(
                        app,
                        "dependency_health",
                        return_value={
                            "status": "repair_required",
                            "installed": {
                                "certifi": "2026.6.17",
                                "yt-dlp": "2026.7.4",
                            },
                            "unexpected_distributions": {
                                "private-environment-package": "9.9.9"
                            },
                        },
                    ),
                    mock.patch.object(app, "yt_dlp_version", return_value="not-tested"),
                    mock.patch.object(app, "ffmpeg_snapshot", return_value={"available": False}),
                ):
                    export_path = app.create_support_export(config_path, quiet=True)
            finally:
                for name, value in original.items():
                    setattr(app, name, value)

            self.assertIsNotNone(export_path)
            with zipfile.ZipFile(export_path, "r") as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"support_summary.json", "config_redacted.json"},
                )
                combined = "\n".join(
                    archive.read(name).decode("utf-8") for name in archive.namelist()
                )
                summary = json.loads(archive.read("support_summary.json"))
            self.assertNotIn("example.org/private", combined)
            self.assertNotIn(str(Path("C:/") / "Users" / "Example"), combined)
            self.assertNotIn("private-environment-package", combined)
            self.assertNotIn("9.9.9", combined)
            for marker in sensitive_markers:
                self.assertNotIn(marker, combined)
            self.assertIn("CONFIGURED_PATH_REDACTED", combined)
            self.assertNotIn("queue", summary)
            self.assertNotIn("recent_log_tail", summary)
            self.assertNotIn("recent_history_tail", summary)
            self.assertEqual(
                summary["dependency_health"]["unexpected_distribution_count"],
                1,
            )
            self.assertFalse(
                summary["safety_boundaries"]["initial_url_private_address_preflight"]
            )
            self.assertFalse(
                summary["safety_boundaries"]["downstream_request_containment"]
            )
            self.assertTrue(Path(str(export_path) + ".sha256.txt").is_file())


if __name__ == "__main__":
    unittest.main()
