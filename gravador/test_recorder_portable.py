import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from recorder import Recorder, parse_playlist


class PortableRecorderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.hls = self.directory / "hls"
        self.hls.mkdir()
        self.recorder = Recorder({
            "hls": str(self.hls),
            "archive": str(self.directory / "archive"),
            "max_bytes": 1024**3,
            "reserve_bytes": 0,
            "idle_seconds": 15,
            "part_seconds": 900,
        })
        self.addCleanup(self.recorder.pool.shutdown)
        self.stream_id = "c" * 32
        self.segment = self.hls / f"{self.stream_id}-0.ts"
        self.manifest = self.hls / f"{self.stream_id}.m3u8"
        self.manifest.write_text(f"#EXTM3U\n#EXTINF:1,\n{self.segment.name}\n")

    def capture(self, data=b"first"):
        self.segment.write_bytes(data)
        self.recorder.capture(self.manifest)

    def test_safe_source_files(self):
        outside = self.directory / "unrelated"
        outside.write_bytes(b"data")
        self.segment.symlink_to(outside)
        self.recorder.capture(self.manifest)
        self.assertFalse(self.recorder.sessions)
        self.manifest.unlink()
        self.manifest.symlink_to(outside)
        with self.assertRaises(OSError):
            parse_playlist(self.manifest)

    def test_restart_reuses_segment_name(self):
        self.capture()
        self.recorder.close(self.stream_id, "stream_idle")
        self.capture(b"another publisher session")
        self.assertIn(self.stream_id, self.recorder.sessions)
        self.assertEqual(len(list(self.recorder.root.rglob("manifest.json"))), 2)

    def test_invalid_manifest_does_not_block_other_exports(self):
        self.capture()
        self.recorder.close(self.stream_id, "stream_idle")
        bad = self.recorder.root / "aaa" / "2020-01-01" / ("d" * 32)
        bad.mkdir(parents=True)
        (bad / "manifest.json").write_text("broken")
        self.recorder.metadata[self.stream_id] = {"channel_id": "known"}
        with patch.object(self.recorder, "export") as export:
            self.recorder.exports()
            for item in self.recorder.jobs.values():
                item.result()
            export.assert_called_once()

    def test_metadata_recovers_before_export(self):
        self.capture()
        self.recorder.close(self.stream_id, "stream_idle")
        self.recorder.exports()
        self.assertFalse(self.recorder.jobs)
        path = next(self.recorder.root.rglob("manifest.json"))
        self.assertTrue((path.parent / "segments").exists())
        self.recorder.metadata[self.stream_id] = {"channel_id": "known"}
        with patch.object(self.recorder, "export") as export:
            self.recorder.next_export = 0
            self.recorder.exports()
            self.recorder.jobs[path].result()
            export.assert_called_once()
            self.assertEqual(json.loads(path.read_text())["channel_id"], "known")

    def test_partial_export_released_after_restart(self):
        self.capture()
        self.recorder.close(self.stream_id, "stream_idle")
        manifest = next(self.recorder.root.rglob("manifest.json"))
        partial = manifest.parent / "export.partial.mp4"
        partial.write_bytes(b"incomplete")
        self.recorder.recover()
        self.assertFalse(partial.exists())
        self.assertTrue((manifest.parent / "segments" / self.segment.name).is_file())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_remux_and_decode_real_media(self):
        for with_audio in (False, True):
            sid = ("a" if with_audio else "b") * 32
            target = self.hls / f"{sid}.m3u8"
            cmd = [
                "ffmpeg", "-loglevel", "error", "-nostdin", "-f", "lavfi",
                "-i", "testsrc2=size=160x120:rate=10",
            ]
            if with_audio:
                cmd.extend(["-f", "lavfi", "-i", "sine=frequency=500:sample_rate=44100", "-c:a", "aac"])
            cmd.extend([
                "-t", "3", "-threads", "1", "-c:v", "libx264", "-g", "10",
                "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
                "-hls_segment_filename", str(self.hls / f"{sid}-%d.ts"), str(target),
            ])
            subprocess.run(cmd, check=True, timeout=45)
            self.recorder.metadata[sid] = {"environment_slug": "sma1", "channel_name": "Ensaio", "slot": 1}
            self.recorder.capture(target)
            folder = self.recorder.sessions[sid]["folder"]
            self.recorder.close(sid, "stream_idle")
            path = folder / "manifest.json"
            self.recorder.export(path)
            data = json.loads(path.read_text())
            self.assertEqual(data["status"], "complete")
            self.assertEqual(data["audio_status"], "present" if with_audio else "not_detected")
            self.assertAlmostEqual(data["duration_seconds"], 3, delta=0.15)
            self.assertIn("UTC-04", data["video_file"])
            self.assertIsNone(data["operator_name"])
            mp4 = folder / data["video_file"]
            subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(mp4), "-f", "null", "-"],
                           check=True, timeout=45)
            self.assertFalse((folder / "segments").exists())


if __name__ == "__main__":
    unittest.main()
