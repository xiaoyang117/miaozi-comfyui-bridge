import argparse
import filecmp
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
REPOSITORY = "miaozi-comfyui-bridge"
ARCHIVE_URL = (
    "https://github.com/xiaoyang117/miaozi-comfyui-bridge/"
    "archive/refs/heads/main.zip"
)
MAX_DOWNLOAD = 100 * 1024 * 1024
MAX_EXTRACTED = 500 * 1024 * 1024


class UpdateError(RuntimeError):
    pass


def validate_proxy(proxy):
    if not proxy:
        return
    try:
        parsed = urlsplit(proxy)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and parsed.port != 0 and parsed.path in ("", "/")
                 and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise UpdateError("代理地址应为 HTTP(S) 地址，例如 http://127.0.0.1:7890")


def update_git(root, proxy):
    command = ["git", "-C", str(root)]
    branch = subprocess.run(
        command + ["branch", "--show-current"], check=True,
        capture_output=True, text=True).stdout.strip()
    if branch != "main":
        raise UpdateError("Git 项目当前不在 main 分支；请切换到 main 后再更新")
    changed = subprocess.run(
        command + ["status", "--porcelain", "--untracked-files=no"],
        check=True, capture_output=True, text=True).stdout.strip()
    if changed:
        raise UpdateError("检测到已修改的项目文件；请先保存或提交改动，再更新")
    env = os.environ.copy()
    if proxy:
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.proxy",
            "GIT_CONFIG_VALUE_0": proxy,
        })
    subprocess.run(command + ["pull", "--ff-only", "origin", "main"],
                   check=True, env=env)
    print("Git 快进更新完成；本地配置和会话数据未修改。")


def download_archive(destination, proxy):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "MiaoZi-Updater"})
    with opener.open(request, timeout=60) as response, destination.open("wb") as output:
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise UpdateError("下载的更新包过大，已停止更新")
            output.write(chunk)


def archive_files(archive):
    files = []
    seen = set()
    extracted_size = 0
    for info in archive.infolist():
        parts = info.filename.rstrip("/").split("/")
        if (not parts or parts[0] != f"{REPOSITORY}-main"
                or any(part in ("", ".", "..") or "\\" in part or ":" in part
                       for part in parts)):
            raise UpdateError("更新包包含不安全的路径")
        mode = (info.external_attr >> 16) & 0o170000
        if mode not in (0, 0o040000, 0o100000):
            raise UpdateError("更新包包含不支持的文件类型")
        if info.is_dir():
            continue
        if len(parts) < 2:
            raise UpdateError("更新包结构不正确")
        relative = PurePosixPath(*parts[1:])
        if relative in seen:
            raise UpdateError("更新包包含重复的文件")
        seen.add(relative)
        extracted_size += info.file_size
        if extracted_size > MAX_EXTRACTED:
            raise UpdateError("更新包解压后过大，已停止更新")
        if (parts[1] in (".git", ".miaozi-update-backups")
                or parts[1:3] in (["server", "data"], ["server", "outputs"])
                or str(relative) in (
                    "server/character_lookup/characters.db",
                    "server/character_lookup/danbooru_character.csv")):
            continue
        files.append((info, Path(*relative.parts)))
    required = {Path(name) for name in (
        "README.md", "start.bat", "server/app.py", "update.py")}
    if not required.issubset({path for _, path in files}):
        raise UpdateError("更新包缺少必要的项目文件")
    return files


def install_archive(root, archive_path):
    with tempfile.TemporaryDirectory(prefix=".miaozi-update-", dir=root) as temp:
        stage = Path(temp) / "new"
        with zipfile.ZipFile(archive_path) as archive:
            files = archive_files(archive)
            for info, relative in files:
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
        changed = []
        for _, relative in files:
            destination = root / relative
            if (destination.is_symlink()
                    or not destination.resolve().is_relative_to(root.resolve())
                    or any((root / parent).is_symlink() for parent in
                           relative.parents if parent != Path("."))
                    or destination.is_dir()):
                raise UpdateError(f"项目路径不安全或被目录占用：{relative}")
            if not destination.is_file() or not filecmp.cmp(
                    stage / relative, destination, shallow=False):
                changed.append(relative)
        if not changed:
            return 0, None
        backup_root = root / ".miaozi-update-backups"
        if backup_root.is_symlink():
            raise UpdateError("备份目录不能是快捷链接")
        backup_root.mkdir(exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix="update-", dir=backup_root))
        applied = []
        try:
            for relative in changed:
                destination = root / relative
                original = backup / relative if destination.exists() else None
                if original:
                    original.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, original)
                destination.parent.mkdir(parents=True, exist_ok=True)
                applied.append((destination, original))
                os.replace(stage / relative, destination)
        except OSError as error:
            try:
                for destination, original in reversed(applied):
                    if original:
                        shutil.copy2(original, destination)
                    else:
                        destination.unlink(missing_ok=True)
            except OSError as rollback_error:
                raise UpdateError(
                    f"更新失败且自动恢复失败：{rollback_error}；备份在 {backup}"
                ) from error
            raise UpdateError(f"更新失败，已恢复原文件：{error}") from error
        return len(changed), backup


def update_zip(root, proxy):
    with tempfile.TemporaryDirectory(prefix=".miaozi-download-", dir=root) as temp:
        archive = Path(temp) / "source.zip"
        download_archive(archive, proxy)
        count, backup = install_archive(root, archive)
    print(f"已更新 {count} 个文件；配置、历史、图片和角色库未修改。")
    if backup:
        print(f"被替换的旧文件备份在 {backup}")


def main():
    parser = argparse.ArgumentParser(description="更新喵梓项目（支持 HTTP(S) 代理）")
    parser.add_argument("--proxy", default=os.environ.get("MIAOZI_UPDATE_PROXY", "").strip(),
                        help="本次更新使用的 HTTP(S) 代理，例如 http://127.0.0.1:7890")
    args = parser.parse_args()
    try:
        validate_proxy(args.proxy)
        if (ROOT / ".git").exists():
            update_git(ROOT, args.proxy)
        else:
            update_zip(ROOT, args.proxy)
    except (UpdateError, OSError, subprocess.CalledProcessError,
            zipfile.BadZipFile) as error:
        parser.exit(1, f"更新失败：{error}\n")


if __name__ == "__main__":
    main()
