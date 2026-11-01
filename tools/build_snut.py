"""Buduje pulpit /snut/ z wersjonowaną konfiguracją przypiętej wersji Homera."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CUSTOMIZATION = ROOT / "integrations" / "homer"
DEFAULT_OUTPUT = ROOT / ".deployment" / "snut-build"


def run(*command, cwd=None):
    executable = shutil.which(command[0])
    if not executable:
        raise RuntimeError(f"Brak wymaganego programu: {command[0]}")
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        invocation = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", subprocess.list2cmdline(command)]
    else:
        invocation = list(command)
    subprocess.run(invocation, cwd=cwd, check=True)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_commit():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_dirty():
    try:
        return bool(subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True
        ).strip())
    except (OSError, subprocess.CalledProcessError):
        return None


def copy_customization(source):
    translations = json.loads((CUSTOMIZATION / "polish-ui.json").read_text(encoding="utf-8"))
    for relative, replacements in translations.items():
        target = source / relative
        text = target.read_text(encoding="utf-8")
        for original, translated in replacements:
            if text.count(original) != 1:
                raise RuntimeError(f"Tłumaczenie Homera wymaga przeglądu: {relative}: {original}")
            text = text.replace(original, translated)
        target.write_text(text, encoding="utf-8")
    shell = source / "src" / "App.vue"
    shell_text = shell.read_text(encoding="utf-8")
    shell_changes = json.loads((CUSTOMIZATION / "header-ui.json").read_text(encoding="utf-8"))
    for original, customized in shell_changes:
        if shell_text.count(original) != 1:
            raise RuntimeError("Nagłówek Homera wymaga przeglądu przypiętej wersji")
        shell_text = shell_text.replace(original, customized)
    shell.write_text(shell_text, encoding="utf-8")
    accessibility = json.loads((CUSTOMIZATION / "accessibility-ui.json").read_text(encoding="utf-8"))
    for relative, replacements in accessibility.items():
        target = source / relative
        text = target.read_text(encoding="utf-8")
        for original, customized in replacements:
            if text.count(original) != 1:
                raise RuntimeError(f"Dostosowanie dostępności Homera wymaga przeglądu: {relative}")
            text = text.replace(original, customized)
        target.write_text(text, encoding="utf-8")
    shutil.copy2(CUSTOMIZATION / "OsdHeader.vue", source / "src" / "components")
    shutil.copy2(ROOT / "web" / "app-header.css", source / "src" / "assets")
    shutil.copy2(ROOT / "web" / "app-header.js", source / "src" / "assets")
    shutil.copy2(ROOT / "web" / "event-view.js", source / "src" / "components" / "services" / "event-view.js")
    shutil.copy2(ROOT / "web" / "match-explanation.js", source / "src" / "components" / "services" / "match-explanation.js")
    shutil.copy2(CUSTOMIZATION / "OsdMonitor.vue", source / "src" / "components" / "services")
    shutil.copy2(CUSTOMIZATION / "OsdEventDetails.vue", source / "src" / "components" / "services")
    shutil.copy2(CUSTOMIZATION / "osd-summary.js", source / "src" / "components" / "services")
    shutil.copy2(CUSTOMIZATION / "config.yml", source / "public" / "assets" / "config.yml")
    shutil.copy2(CUSTOMIZATION / "custom.css", source / "public" / "assets" / "custom.css")
    shutil.copy2(CUSTOMIZATION / "vite.config.js", source / "vite.config.js")


def validate_build(output):
    required = [
        output / "index.html",
        output / "assets" / "config.yml",
        output / "assets" / "custom.css",
        output / "VERSION",
    ]
    missing = [str(path.relative_to(output)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"Brak plików zbudowanej aplikacji: {', '.join(missing)}")
    html = (output / "index.html").read_text(encoding="utf-8")
    if 'src="/snut/resources/' not in html:
        raise RuntimeError("Zbudowana aplikacja nie używa bazowej ścieżki /snut/")
    forbidden = [path.name for path in output.rglob("*") if path.is_file() and (
        path.name.startswith("sw.") or path.name in {"sw.js", "service-worker.js", "manifest.webmanifest"}
    )]
    if forbidden:
        raise RuntimeError(f"Zbudowana aplikacja zawiera wyłączone zasoby PWA lub service worker: {forbidden}")


def write_release_metadata(output, upstream, release_id=None):
    metadata = {
        "release_id": release_id or datetime.now(timezone.utc).strftime("snut-%Y%m%dT%H%M%SZ"),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_commit": git_commit(),
        "project_dirty": git_dirty(),
        "homer_tag": upstream["tag"],
        "homer_commit": upstream["commit"],
    }
    (output / "release.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    files = []
    for path in sorted(item for item in output.rglob("*") if item.is_file()):
        if path.name == "manifest.sha256.json":
            continue
        files.append({
            "path": path.relative_to(output).as_posix(),
            "sha256": sha256(path),
            "size": path.stat().st_size,
        })
    (output / "manifest.sha256.json").write_text(
        json.dumps({"release": metadata, "files": files}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build(output, source_override=None, release_id=None):
    upstream = json.loads((CUSTOMIZATION / "upstream.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="snut-homer-") as temporary:
        source = Path(temporary) / "homer"
        if source_override:
            shutil.copytree(Path(source_override), source, ignore=shutil.ignore_patterns("node_modules", "dist"))
        else:
            run("git", "clone", "--filter=blob:none", "--no-checkout", upstream["repository"], str(source))
            run("git", "fetch", "--depth", "1", "origin", upstream["commit"], cwd=source)
            run("git", "checkout", "--detach", upstream["commit"], cwd=source)
        actual_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip()
        if actual_commit != upstream["commit"]:
            raise RuntimeError(f"Nieoczekiwany commit Homera: {actual_commit}")

        copy_customization(source)
        run("corepack", f"pnpm@{upstream['pnpm']}", "install", "--frozen-lockfile", cwd=source)
        run(
            "corepack", f"pnpm@{upstream['pnpm']}", "exec", "eslint",
            "src/App.vue",
            "src/components/Service.vue",
            "src/components/SettingToggle.vue",
            "src/components/DarkMode.vue",
            "src/components/OsdHeader.vue",
            "src/components/services/OsdMonitor.vue",
            "src/components/services/OsdEventDetails.vue",
            "src/components/services/osd-summary.js",
            cwd=source,
        )
        run("corepack", f"pnpm@{upstream['pnpm']}", "build", cwd=source)

        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(source / "dist", output)
        shutil.copy2(source / 'LICENSE', output / 'HOMER-LICENSE')
        for notice in source.glob('NOTICE*'):
            if notice.is_file():
                shutil.copy2(notice, output / notice.name)
        validate_build(output)
        write_release_metadata(output, upstream, release_id=release_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source", type=Path, help="Lokalny katalog roboczy Homera do testowego budowania")
    parser.add_argument("--release-id", help="Jawny identyfikator wydania SNUT")
    args = parser.parse_args()
    build(args.output.resolve(), args.source.resolve() if args.source else None, args.release_id)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
