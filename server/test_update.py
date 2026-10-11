import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

import update


class UpdateTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.write("README.md", "old readme")
        self.write("start.bat", "old launcher")
        self.write("update.py", "old updater")
        self.write("server/app.py", "old app")
        self.write("server/data/settings.json", '{"api_key":"private"}')
        self.write("server/data/sessions.db", "private conversations")
        self.write("server/outputs/image.png", "private image")
        self.write("server/character_lookup/characters.db", "private characters")
        self.write("workflows/custom.json", "my workflow")

    def write(self, name, content):
        file = self.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")

    def archive(self, additional=()):
        file = self.root / "source.zip"
        with zipfile.ZipFile(file, "w") as archive:
            for name in ("README.md", "start.bat", "update.py", "server/app.py"):
                archive.writestr(f"{update.REPOSITORY}-main/{name}", f"new {name}")
            for name, content in additional:
                archive.writestr(f"{update.REPOSITORY}-main/{name}", content)
        return file

    def test_zip_updates_files_and_keeps_data_and_backup(self):
        source = self.archive([
            ("server/data/settings.json", "not my settings"),
            ("server/outputs/image.png", "not my image"),
            ("server/character_lookup/characters.db", "not my characters"),
            ("workflows/sample.json", "new sample"),
        ])
        count, backup = update.install_archive(self.root, source)
        self.assertEqual(count, 5)
        self.assertEqual((self.root / "README.md").read_text(), "new README.md")
        self.assertEqual((backup / "README.md").read_text(), "old readme")
        self.assertEqual((backup / "server/app.py").read_text(), "old app")
        for name, content in (
            ("server/data/settings.json", '{"api_key":"private"}'),
            ("server/data/sessions.db", "private conversations"),
            ("server/outputs/image.png", "private image"),
            ("server/character_lookup/characters.db", "private characters"),
            ("workflows/custom.json", "my workflow"),
        ):
            with self.subTest(name=name):
                self.assertEqual((self.root / name).read_text(), content)
        self.assertEqual(update.install_archive(self.root, source), (0, None))

    def test_unsafe_archive_paths_and_symlinks_do_not_update_project(self):
        bad_entries = [
            ("../escape", "bad"),
            ("server/../../escape", "bad"),
            ("server\\data\\settings.json", "bad"),
        ]
        for name, content in bad_entries:
            with self.subTest(name=name):
                with self.assertRaises(update.UpdateError):
                    update.install_archive(self.root, self.archive([(name, content)]))
                self.assertEqual((self.root / "README.md").read_text(), "old readme")

        source = self.archive()
        with zipfile.ZipFile(source, "a") as archive:
            link = zipfile.ZipInfo(f"{update.REPOSITORY}-main/linked")
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            archive.writestr(link, "../server/data")
        with self.assertRaises(update.UpdateError):
            update.install_archive(self.root, source)
        self.assertEqual((self.root / "README.md").read_text(), "old readme")

    def test_backup_restores_previous_files_if_copy_fails(self):
        source = self.archive()
        replace = update.os.replace

        def fail_on_second(source_file, destination):
            if destination == self.root / "server/app.py":
                raise OSError("file in use")
            replace(source_file, destination)

        with patch("update.os.replace", side_effect=fail_on_second):
            with self.assertRaisesRegex(update.UpdateError, "已恢复原文件"):
                update.install_archive(self.root, source)
        self.assertEqual((self.root / "README.md").read_text(), "old readme")
        self.assertEqual((self.root / "server/app.py").read_text(), "old app")

    def test_missing_files_or_unsafe_existing_path_never_update(self):
        archive_path = self.root / "incomplete.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(f"{update.REPOSITORY}-main/README.md", "new readme")
        with self.assertRaises(update.UpdateError):
            update.install_archive(self.root, archive_path)
        self.assertEqual((self.root / "README.md").read_text(), "old readme")
        source = self.archive()
        (self.root / "server/app.py").unlink()
        (self.root / "server/app.py").symlink_to(self.root / "server/data/settings.json")
        with self.assertRaises(update.UpdateError):
            update.install_archive(self.root, source)
        self.assertEqual((self.root / "README.md").read_text(), "old readme")
        self.assertEqual((self.root / "server/data/settings.json").read_text(),
                         '{"api_key":"private"}')

    def test_proxy_configuration_and_git_fast_forward(self):
        for address in ("http://127.0.0.1:7890", "https://proxy.example:443/"):
            update.validate_proxy(address)
        for address in ("socks5://127.0.0.1:10808", "http://localhost:bad",
                        "http://localhost:7890/path"):
            with self.assertRaises(update.UpdateError):
                update.validate_proxy(address)
        proxy = "http://127.0.0.1:7890"
        responses = [CompletedProcess([], 0, stdout="main\n"),
                     CompletedProcess([], 0, stdout=""),
                     CompletedProcess([], 0)]
        with patch("update.subprocess.run", side_effect=responses) as run:
            update.update_git(self.root, proxy)
        self.assertEqual(run.call_args.args[0][-4:],
                         ["pull", "--ff-only", "origin", "main"])
        self.assertEqual(run.call_args.kwargs["env"]["GIT_CONFIG_VALUE_0"], proxy)

    def test_git_refuses_non_main_branch_or_modified_files(self):
        for branch, changes in (("feature", ""), ("main", " M server/app.py\n")):
            with self.subTest(branch=branch, changes=changes):
                responses = [CompletedProcess([], 0, stdout=branch)]
                if branch == "main":
                    responses.append(CompletedProcess([], 0, stdout=changes))
                with patch("update.subprocess.run", side_effect=responses) as run:
                    with self.assertRaises(update.UpdateError):
                        update.update_git(self.root, "")
                self.assertEqual(run.call_count, len(responses))

    def test_download_uses_only_selected_proxy(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b"downloaded")
        destination = self.root / "download.zip"
        with patch("update.urllib.request.build_opener", return_value=opener) as build:
            update.download_archive(destination, "http://127.0.0.1:7890")
        self.assertEqual(build.call_args.args[0].proxies,
                         {"http": "http://127.0.0.1:7890",
                          "https": "http://127.0.0.1:7890"})
        self.assertEqual(destination.read_bytes(), b"downloaded")


if __name__ == "__main__":
    unittest.main()
