import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

import scripts.archive_security as archive_security
from scripts.archive_security import (
    UnsafeArchive,
    extract_archive,
    safe_output_path,
    validate_archive,
)


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = {
    "project/.env": b"operator config\n",
    "project/config/headscale/policy.json": b"{}\n",
    "project/runtime/headscale/config.yaml": b"server_url: http://localhost\n",
    "project/runtime/headscale/extra_records.json": b"[]\n",
    "project/runtime/headplane/config.yaml": b"server:\n",
    "project/runtime/headplane/cookie-secret": b"secret\n",
    "project/runtime/headplane/api-key": b"secret\n",
}


def volume_tar(extra=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo("state.db")
        member.mode = 0o600
        member.size = 4
        archive.addfile(member, io.BytesIO(b"data"))
        if extra is not None:
            archive.addfile(extra, io.BytesIO(b"x") if extra.isfile() else None)
    return output.getvalue()


def make_archive(path, malicious=None, nested_malicious=None):
    with tarfile.open(path, mode="w:gz") as archive:
        manifest = json.dumps({"format": "woow-headscale-backup-v1"}).encode()
        member = tarfile.TarInfo("manifest.json")
        member.size = len(manifest)
        archive.addfile(member, io.BytesIO(manifest))
        for name, payload in REQUIRED_FILES.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        for name in ("volumes/headscale-data.tar", "volumes/headplane-data.tar"):
            payload = volume_tar(nested_malicious if name.startswith("volumes/headscale") else None)
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        if malicious is not None:
            archive.addfile(malicious, io.BytesIO(b"x") if malicious.isfile() else None)
    os.chmod(path, 0o600)


class ArchiveSafetyTests(unittest.TestCase):
    def test_valid_archive_extracts_only_private_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "backup.tar.gz"
            destination = Path(temporary) / "out"
            make_archive(archive)
            manifest = validate_archive(archive)
            self.assertEqual(manifest["format"], "woow-headscale-backup-v1")
            extract_archive(archive, destination)
            self.assertEqual((destination / "project/.env").read_bytes(), b"operator config\n")
            self.assertEqual((destination / "project/.env").stat().st_mode & 0o777, 0o600)

    def test_rejects_absolute_traversal_links_and_special_members(self):
        cases = []
        for name in ("/absolute", "../escape", "safe/../../escape"):
            member = tarfile.TarInfo(name)
            member.size = 1
            cases.append(member)
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            member = tarfile.TarInfo("unsafe-special")
            member.type = kind
            member.linkname = "target"
            cases.append(member)
        with tempfile.TemporaryDirectory() as temporary:
            for index, member in enumerate(cases):
                with self.subTest(member=member.name, kind=member.type):
                    archive = Path(temporary) / f"bad-{index}.tar.gz"
                    make_archive(archive, malicious=member)
                    with self.assertRaises(UnsafeArchive):
                        validate_archive(archive)

    def test_rejects_unsafe_nested_volume_member(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "bad-volume.tar.gz"
            member = tarfile.TarInfo("../../host")
            member.size = 1
            make_archive(archive, nested_malicious=member)
            with self.assertRaises(UnsafeArchive):
                validate_archive(archive)

    def test_output_destination_rejects_symlink_components_and_existing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary) / "private"
            private.mkdir(mode=0o700)
            selected = safe_output_path(private, "new.tar.gz")
            self.assertEqual(selected, private / "new.tar.gz")
            selected.touch(mode=0o600)
            with self.assertRaises(ValueError):
                safe_output_path(selected, "unused.tar.gz")
            link = Path(temporary) / "linked"
            link.symlink_to(private, target_is_directory=True)
            with self.assertRaises(ValueError):
                safe_output_path(link / "another.tar.gz", "unused.tar.gz")

    def test_rejects_symlink_archive_and_group_writable_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "backup.tar.gz"
            make_archive(archive)
            os.chmod(archive, 0o620)
            with self.assertRaises(UnsafeArchive):
                validate_archive(archive)
            os.chmod(archive, 0o600)
            link = Path(temporary) / "link.tar.gz"
            link.symlink_to(archive)
            with self.assertRaises(UnsafeArchive):
                validate_archive(link)

    def test_path_replacement_cannot_change_extracted_inode_or_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "selected.tar.gz"
            replacement = Path(temporary) / "replacement.tar.gz"
            destination = Path(temporary) / "out"
            escaped = Path(temporary) / "escaped"
            make_archive(archive)
            traversal = tarfile.TarInfo("../escaped")
            traversal.size = 1
            make_archive(replacement, malicious=traversal)
            original_validate = archive_security._validate_outer_archive

            def validate_then_replace(open_archive):
                manifest = original_validate(open_archive)
                os.replace(replacement, archive)
                return manifest

            with mock.patch.object(
                archive_security, "_validate_outer_archive", side_effect=validate_then_replace
            ):
                extract_archive(archive, destination)

            self.assertEqual(
                (destination / "project/.env").read_bytes(), b"operator config\n"
            )
            self.assertFalse(escaped.exists())


class MaintenanceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backup = (ROOT / "scripts/backup.sh").read_text(encoding="utf-8")
        cls.restore = (ROOT / "scripts/restore.sh").read_text(encoding="utf-8")
        cls.remove = (ROOT / "scripts/remove.sh").read_text(encoding="utf-8")

    def test_all_shell_scripts_parse(self):
        scripts = ["deploy.sh", "scripts/verify.sh", "scripts/backup.sh", "scripts/restore.sh", "scripts/remove.sh"]
        result = subprocess.run(["bash", "-n", *scripts], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_backup_exit_cleanup_restarts_and_verifies_after_stop(self):
        self.assertIn("trap cleanup EXIT", self.backup)
        stop = self.backup.index('"$PROJECT_DIR/deploy.sh" --stop')
        stopped = self.backup.index("STACK_STOPPED=true")
        self.assertLess(stopped, stop)
        cleanup = self.backup.index("cleanup()")
        self.assertIn('"$PROJECT_DIR/deploy.sh" >/dev/null', self.backup[cleanup:stop])
        self.assertIn('"$PROJECT_DIR/scripts/verify.sh" >/dev/null', self.backup[cleanup:stop])
        self.assertIn("os.link(source, destination)", self.backup)
        self.assertNotIn('ln -- "$TEMP_ARCHIVE" "$FINAL_ARCHIVE"', self.backup)
        self.assertIn("trap '' INT TERM HUP", self.backup[cleanup:stop])

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            fake_bin = Path(temporary) / "bin"
            output = Path(temporary) / "output"
            for directory in (
                project / "scripts", project / "config/headscale",
                project / "runtime/headscale", project / "runtime/headplane",
                fake_bin, output,
            ):
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(ROOT / "scripts/backup.sh", project / "scripts/backup.sh")
            shutil.copy2(ROOT / "scripts/archive_security.py", project / "scripts/archive_security.py")
            shutil.copy2(ROOT / "scripts/volume_ownership.py", project / "scripts/volume_ownership.py")
            shutil.copy2(ROOT / "scripts/runtime_config.py", project / "scripts/runtime_config.py")
            for relative in (
                ".env", "config/headscale/policy.json", "runtime/headscale/config.yaml",
                "runtime/headscale/extra_records.json", "runtime/headplane/config.yaml",
                "runtime/headplane/cookie-secret", "runtime/headplane/api-key",
            ):
                target = project / relative
                if relative == ".env":
                    shutil.copy2(ROOT / ".env.example", target)
                else:
                    target.write_text("{}\n", encoding="utf-8")
                target.chmod(0o600)
            log = Path(temporary) / "calls"
            (project / "deploy.sh").write_text(
                f"#!/bin/bash\necho deploy:$* >> {log}\nexit 0\n", encoding="utf-8"
            )
            (project / "scripts/verify.sh").write_text(
                f"#!/bin/bash\necho verify >> {log}\nexit 0\n", encoding="utf-8"
            )
            podman = fake_bin / "podman"
            podman.write_text(
                "#!/bin/bash\n"
                "if [[ $1 == container && $2 == exists ]]; then exit 0; fi\n"
                "if [[ $1 == inspect ]]; then\n"
                "  if [[ $2 == headscale ]]; then dest=/var/lib/headscale; name=woow_headscale_headscale-data; "
                "else dest=/var/lib/headplane; name=woow_headscale_headplane-data; fi\n"
                "  printf '[{\"Config\":{\"Labels\":{\"org.woow-headscale.project\":\"woow-headscale\",\"org.woow-headscale.project-dir\":\"%s\",\"com.docker.compose.project\":\"woow_headscale\",\"io.podman.compose.project\":\"woow_headscale\"}},'"
                "'\"Mounts\":[{\"Destination\":\"%s\",\"Type\":\"volume\",\"Name\":\"%s\"}]}]' \"$PWD\" \"$dest\" \"$name\"\n"
                "  exit 0\n"
                "fi\n"
                "if [[ $1 == volume && $2 == ls ]]; then printf 'woow_headscale_headscale-data\\nwoow_headscale_headplane-data\\n'; exit 0; fi\n"
                "if [[ $1 == volume && $2 == inspect ]]; then "
                "printf '[{\"Name\":\"%s\",\"Labels\":{\"org.woow-headscale.project\":\"woow-headscale\",\"org.woow-headscale.project-dir\":\"%s\",\"com.docker.compose.project\":\"woow_headscale\",\"io.podman.compose.project\":\"woow_headscale\"}}]' \"$3\" \"$PWD\"; exit 0; fi\n"
                "if [[ $1 == volume && $2 == export ]]; then exit 17; fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            (fake_bin / "podman-compose").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            for executable in (project / "deploy.sh", project / "scripts/verify.sh", project / "scripts/backup.sh", project / "scripts/archive_security.py", podman, fake_bin / "podman-compose"):
                executable.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
            result = subprocess.run(
                [str(project / "scripts/backup.sh"), "--output", str(output)],
                cwd=project, env=environment, text=True, capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["deploy:--stop", "deploy:", "verify"])

    def _backup_fixture(self, temporary):
        base = Path(temporary)
        project = base / "project"
        fake_bin = base / "bin"
        output = base / "output"
        for directory in (
            project / "scripts", project / "config/headscale",
            project / "runtime/headscale", project / "runtime/headplane",
            fake_bin, output,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for script in ("backup.sh", "archive_security.py", "volume_ownership.py", "runtime_config.py"):
            shutil.copy2(ROOT / "scripts" / script, project / "scripts" / script)
        for relative in (
            ".env", "config/headscale/policy.json", "runtime/headscale/config.yaml",
            "runtime/headscale/extra_records.json", "runtime/headplane/config.yaml",
            "runtime/headplane/cookie-secret", "runtime/headplane/api-key",
        ):
            target = project / relative
            if relative == ".env":
                shutil.copy2(ROOT / ".env.example", target)
            else:
                target.write_text("{}\n", encoding="utf-8")
            target.chmod(0o600)
        log = base / "calls.log"
        (project / "deploy.sh").write_text(
            "#!/bin/bash\necho deploy:$* >> \"$TEST_LOG\"\n"
            "if [[ ${1:-} != --stop && ${SIGNAL_CLEANUP:-false} == true ]]; then "
            "kill -TERM \"$PPID\"; kill -INT \"$PPID\"; sleep 0.1; fi\nexit 0\n",
            encoding="utf-8",
        )
        (project / "scripts/verify.sh").write_text(
            "#!/bin/bash\necho verify >> \"$TEST_LOG\"\nexit 0\n", encoding="utf-8"
        )
        (fake_bin / "date").write_text(
            "#!/bin/bash\nprintf 20260827T010203Z\n", encoding="utf-8"
        )
        (fake_bin / "rm").write_text(
            "#!/bin/bash\n"
            "if [[ ${SIGNAL_CLEANUP:-false} == true && $* == *'.backup-stage.'* ]]; then "
            "kill -HUP \"$PPID\"; kill -TERM \"$PPID\"; fi\n"
            f"exec {shutil.which('rm')} \"$@\"\n",
            encoding="utf-8",
        )
        (fake_bin / "podman-compose").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        (fake_bin / "podman").write_text(
            "#!/bin/bash\n"
            "echo podman:$* >> \"$TEST_LOG\"\n"
            "if [[ $1 == container && $2 == exists ]]; then "
            "[[ ${CONTAINERS_PRESENT:-false} == true ]]; exit $?; fi\n"
            "if [[ $1 == inspect ]]; then\n"
            "  if [[ $2 == headscale ]]; then dest=/var/lib/headscale; logical=headscale-data; "
            "else dest=/var/lib/headplane; logical=headplane-data; fi\n"
            "  mount=woow_headscale_$logical; [[ -z ${WRONG_MOUNT:-} ]] || mount=$WRONG_MOUNT\n"
            "  label=${CONTAINER_PROJECT:-woow_headscale}\n"
            "  printf '[{\"Config\":{\"Labels\":{\"org.woow-headscale.project\":\"woow-headscale\",\"org.woow-headscale.project-dir\":\"%s\",\"com.docker.compose.project\":\"%s\",\"io.podman.compose.project\":\"%s\"}},\"Mounts\":[{\"Type\":\"volume\",\"Destination\":\"%s\",\"Name\":\"%s\"}]}]' \"$PWD\" \"$label\" \"$label\" \"$dest\" \"$mount\"\n"
            "  exit 0\n"
            "fi\n"
            "if [[ $1 == volume && $2 == ls ]]; then cat \"$VOLUME_LIST\"; exit 0; fi\n"
            "if [[ $1 == volume && $2 == inspect ]]; then\n"
            "  label=${VOLUME_PROJECT:-woow_headscale}\n"
            "  printf '[{\"Name\":\"%s\",\"Labels\":{\"org.woow-headscale.project\":\"woow-headscale\",\"org.woow-headscale.project-dir\":\"%s\",\"com.docker.compose.project\":\"%s\",\"io.podman.compose.project\":\"%s\"}}]' \"$3\" \"$PWD\" \"$label\" \"$label\"\n"
            "  exit 0\n"
            "fi\n"
            "if [[ $1 == volume && $2 == export ]]; then\n"
            "  if [[ -n ${RACE_DEST:-} && ! -e $RACE_DEST && ! -L $RACE_DEST ]]; then\n"
            "    case ${RACE_KIND:-regular} in\n"
            "      regular) printf racer > \"$RACE_DEST\"; chmod 600 \"$RACE_DEST\" ;;\n"
            "      directory) mkdir \"$RACE_DEST\" ;;\n"
            "      symlink-directory) mkdir \"$RACE_LINK_TARGET\"; ln -s \"$RACE_LINK_TARGET\" \"$RACE_DEST\" ;;\n"
            "    esac\n"
            "  fi\n"
            "  sleep ${EXPORT_DELAY:-0}\n"
            "  empty=$(mktemp -d); printf state > \"$empty/state.db\"; tar -cf \"$5\" -C \"$empty\" state.db; rm -rf \"$empty\"; exit 0\n"
            "fi\nexit 1\n",
            encoding="utf-8",
        )
        for executable in (
            project / "deploy.sh", project / "scripts/verify.sh", project / "scripts/backup.sh",
            project / "scripts/archive_security.py", project / "scripts/volume_ownership.py",
            fake_bin / "date", fake_bin / "rm", fake_bin / "podman", fake_bin / "podman-compose",
        ):
            executable.chmod(0o755)
        volume_list = base / "volumes"
        environment = os.environ.copy()
        environment.update({
            "PATH": str(fake_bin) + os.pathsep + environment["PATH"],
            "TEST_LOG": str(log), "VOLUME_LIST": str(volume_list),
        })
        return project, output, volume_list, log, environment

    def test_backup_absent_containers_ignores_suffix_volume_and_selects_exact_labelled_volumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, output, volumes, log, environment = self._backup_fixture(temporary)
            volumes.write_text(
                "unrelated_headscale-data\nwoow_headscale_headscale-data\n"
                "unrelated_headplane-data\nwoow_headscale_headplane-data\n", encoding="utf-8"
            )
            result = subprocess.run(
                [str(project / "scripts/backup.sh"), "--output", str(output)],
                cwd=project, env=environment, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archives = list(output.glob("*.tar.gz"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)
            calls = log.read_text(encoding="utf-8").splitlines()
            exports = [line for line in calls if line.startswith("podman:volume export")]
            self.assertEqual(exports, [
                next(line for line in exports if "woow_headscale_headscale-data" in line),
                next(line for line in exports if "woow_headscale_headplane-data" in line),
            ])
            self.assertNotIn("unrelated_", "\n".join(exports))
            self.assertEqual(list(project.glob(".backup-stage.*")), [])
            self.assertEqual(list(output.glob(".woow-headscale-backup.*")), [])

    def test_backup_refuses_only_suffix_matches_or_mismatched_volume_labels(self):
        for mode in ("suffix-only", "mismatched-label"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                project, output, volumes, log, environment = self._backup_fixture(temporary)
                if mode == "suffix-only":
                    volumes.write_text(
                        "unrelated_headscale-data\nunrelated_headplane-data\n", encoding="utf-8"
                    )
                else:
                    volumes.write_text(
                        "woow_headscale_headscale-data\nwoow_headscale_headplane-data\n", encoding="utf-8"
                    )
                    environment["VOLUME_PROJECT"] = "somebody-else"
                result = subprocess.run(
                    [str(project / "scripts/backup.sh"), "--output", str(output)],
                    cwd=project, env=environment, text=True, capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(list(output.glob("*.tar.gz")))
                calls = log.read_text(encoding="utf-8")
                self.assertNotIn("volume export", calls)
                self.assertNotIn("deploy:--stop", calls)

    def test_backup_fails_closed_for_owned_container_mount_or_project_label_mismatch(self):
        for variable, value in (("WRONG_MOUNT", "other_headscale-data"),
                                ("CONTAINER_PROJECT", "somebody-else")):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as temporary:
                project, output, volumes, log, environment = self._backup_fixture(temporary)
                volumes.write_text("woow_headscale_headscale-data\nwoow_headscale_headplane-data\n", encoding="utf-8")
                environment["CONTAINERS_PRESENT"] = "true"
                environment[variable] = value
                result = subprocess.run(
                    [str(project / "scripts/backup.sh"), "--output", str(output)],
                    cwd=project, env=environment, text=True, capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("volume export", log.read_text(encoding="utf-8"))
                self.assertFalse(list(output.glob("*.tar.gz")))

    def test_backup_atomic_publication_refuses_every_existing_exact_target_type(self):
        for kind in ("regular", "directory", "symlink-directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                project, output, volumes, _, environment = self._backup_fixture(temporary)
                volumes.write_text(
                    "woow_headscale_headscale-data\nwoow_headscale_headplane-data\n", encoding="utf-8"
                )
                destination = output / "woow-headscale-20260827T010203Z.tar.gz"
                link_target = Path(temporary) / "link-target"
                environment.update({
                    "RACE_DEST": str(destination), "RACE_KIND": kind,
                    "RACE_LINK_TARGET": str(link_target),
                })
                result = subprocess.run(
                    [str(project / "scripts/backup.sh"), "--output", str(output)],
                    cwd=project, env=environment, text=True, capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("Backup created:", result.stdout)
                if kind == "regular":
                    self.assertEqual(destination.read_bytes(), b"racer")
                elif kind == "directory":
                    self.assertTrue(destination.is_dir())
                    self.assertEqual(list(destination.iterdir()), [])
                else:
                    self.assertTrue(destination.is_symlink())
                    self.assertEqual(destination.resolve(), link_target)
                    self.assertEqual(list(link_target.iterdir()), [])
                self.assertEqual(list(output.glob(".woow-headscale-backup.*")), [])
                self.assertEqual(list(project.glob(".backup-stage.*")), [])

    def test_concurrent_same_timestamp_backups_publish_exactly_one_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, output, volumes, _, environment = self._backup_fixture(temporary)
            volumes.write_text("woow_headscale_headscale-data\nwoow_headscale_headplane-data\n", encoding="utf-8")
            environment["EXPORT_DELAY"] = "0.2"
            command = [str(project / "scripts/backup.sh"), "--output", str(output)]
            processes = [
                subprocess.Popen(command, cwd=project, env=environment, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for _ in range(2)
            ]
            results = [process.communicate(timeout=20) + (process.returncode,) for process in processes]
            self.assertEqual(sorted(result[2] == 0 for result in results), [False, True], results)
            archives = list(output.glob("*.tar.gz"))
            self.assertEqual(len(archives), 1)
            validate_archive(archives[0])
            self.assertEqual(list(output.glob(".woow-headscale-backup.*")), [])

    def test_backup_cleanup_ignores_repeated_signals_until_restart_and_deletion_finish(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, output, volumes, log, environment = self._backup_fixture(temporary)
            volumes.write_text("woow_headscale_headscale-data\nwoow_headscale_headplane-data\n", encoding="utf-8")
            environment["SIGNAL_CLEANUP"] = "true"
            result = subprocess.run(
                [str(project / "scripts/backup.sh"), "--output", str(output)],
                cwd=project, env=environment, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").splitlines()[-2:], ["deploy:", "verify"])
            self.assertEqual(list(project.glob(".backup-stage.*")), [])
            self.assertEqual(list(output.glob(".woow-headscale-backup.*")), [])

    def test_restore_confirmation_validation_and_rollback_precede_mutation(self):
        confirmation = self.restore.index("--confirm-destructive-restore")
        validation = self.restore.index("archive_security.py\" extract")
        stop = self.restore.index('"$PROJECT_DIR/deploy.sh" --stop', validation)
        mutation_call = self.restore.index('apply_stage "$WORK/requested"', stop)
        self.assertLess(confirmation, validation)
        self.assertLess(validation, stop)
        self.assertLess(stop, mutation_call)
        self.assertNotRegex(self.restore, r"(?m)^\s*(source|\.)\s+.*\.env")
        self.assertIn("automatic rollback", self.restore)

    def test_restore_failure_and_term_signal_both_rollback_before_cleanup(self):
        real_python = shutil.which("python3")
        self.assertIsNotNone(real_python)
        for mode in ("verify-failure", "term-signal"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                project = base / "project"
                fake_bin = base / "bin"
                (project / "scripts").mkdir(parents=True)
                (project / "config/headscale").mkdir(parents=True)
                fake_bin.mkdir()
                shutil.copy2(ROOT / "scripts/restore.sh", project / "scripts/restore.sh")
                shutil.copy2(
                    ROOT / "scripts/archive_security.py", project / "scripts/archive_security.py"
                )
                shutil.copy2(
                    ROOT / "scripts/volume_ownership.py", project / "scripts/volume_ownership.py"
                )
                requested = base / "requested.tar.gz"
                rollback = base / "rollback.tar.gz"
                make_archive(requested)
                make_archive(rollback)
                log = base / "calls.log"
                starts = base / "starts"
                verifies = base / "verifies"

                (project / "scripts/backup.sh").write_text(
                    "#!/bin/bash\ncp -- \"$ROLLBACK_SOURCE\" \"$2\"\nchmod 600 \"$2\"\n",
                    encoding="utf-8",
                )
                (project / "deploy.sh").write_text(
                    "#!/bin/bash\n"
                    "echo deploy:$* >> \"$TEST_LOG\"\n"
                    "if [[ ${1:-} == --stop ]]; then exit 0; fi\n"
                    "count=0; [[ ! -f $START_FILE ]] || count=$(cat \"$START_FILE\")\n"
                    "count=$((count + 1)); echo \"$count\" > \"$START_FILE\"\n"
                    "if [[ $TEST_MODE == term-signal && $count == 1 ]]; then "
                    "kill -TERM \"$PPID\"; sleep 0.2; fi\n"
                    "exit 0\n",
                    encoding="utf-8",
                )
                (project / "scripts/verify.sh").write_text(
                    "#!/bin/bash\n"
                    "echo verify >> \"$TEST_LOG\"\n"
                    "count=0; [[ ! -f $VERIFY_FILE ]] || count=$(cat \"$VERIFY_FILE\")\n"
                    "count=$((count + 1)); echo \"$count\" > \"$VERIFY_FILE\"\n"
                    "if [[ $TEST_MODE == verify-failure && $count == 1 ]]; then exit 9; fi\n"
                    "exit 0\n",
                    encoding="utf-8",
                )
                (fake_bin / "python3").write_text(
                    f"#!/bin/bash\nif [[ $1 == */runtime_config.py ]]; then "
                    f"[[ $2 != project-name ]] || printf woow_headscale; exit 0; fi\n"
                    f"exec {real_python} \"$@\"\n",
                    encoding="utf-8",
                )
                (fake_bin / "podman").write_text(
                    "#!/bin/bash\n"
                    "echo podman:$* >> \"$TEST_LOG\"\n"
                    "if [[ $1 == container && $2 == exists ]]; then exit 0; fi\n"
                    "if [[ $1 == inspect ]]; then\n"
                    "  if [[ $2 == headscale ]]; then dest=/var/lib/headscale; name=woow_headscale_headscale-data; "
                    "else dest=/var/lib/headplane; name=woow_headscale_headplane-data; fi\n"
                    "  printf '[{\"Config\":{\"Labels\":{\"org.woow-headscale.project\":\"woow-headscale\",\"org.woow-headscale.project-dir\":\"%s\",\"com.docker.compose.project\":\"woow_headscale\",\"io.podman.compose.project\":\"woow_headscale\"}},'"
                    "'\"Mounts\":[{\"Destination\":\"%s\",\"Type\":\"volume\",\"Name\":\"%s\"}]}]' \"$PWD\" \"$dest\" \"$name\"\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ $1 == volume && $2 == ls ]]; then printf 'woow_headscale_headscale-data\\nwoow_headscale_headplane-data\\n'; exit 0; fi\n"
                    "if [[ $1 == volume && $2 == inspect ]]; then "
                    "printf '[{\"Name\":\"%s\",\"Labels\":{\"org.woow-headscale.project\":\"woow-headscale\",\"org.woow-headscale.project-dir\":\"%s\",\"com.docker.compose.project\":\"woow_headscale\",\"io.podman.compose.project\":\"woow_headscale\"}}]' \"$3\" \"$PWD\"; exit 0; fi\n"
                    "exit 0\n",
                    encoding="utf-8",
                )
                (fake_bin / "podman-compose").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
                for executable in (
                    project / "scripts/restore.sh", project / "scripts/archive_security.py",
                    project / "scripts/volume_ownership.py", project / "scripts/backup.sh",
                    project / "scripts/verify.sh",
                    project / "deploy.sh", fake_bin / "python3", fake_bin / "podman",
                    fake_bin / "podman-compose",
                ):
                    executable.chmod(0o755)
                environment = os.environ.copy()
                environment.update({
                    "PATH": str(fake_bin) + os.pathsep + environment["PATH"],
                    "ROLLBACK_SOURCE": str(rollback), "TEST_LOG": str(log),
                    "START_FILE": str(starts), "VERIFY_FILE": str(verifies),
                    "TEST_MODE": mode,
                })
                result = subprocess.run(
                    [str(project / "scripts/restore.sh"), "--archive", str(requested),
                     "--confirm-destructive-restore"],
                    cwd=project, env=environment, text=True, capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                calls = log.read_text(encoding="utf-8").splitlines()
                rollback_imports = [
                    line for line in calls if line.startswith("podman:volume import")
                    and "/rollback/" in line
                ]
                self.assertEqual(len(rollback_imports), 2, calls)
                self.assertEqual(calls[-2:], ["deploy:", "verify"])
                self.assertIn("rolled back, restarted, and verified", result.stderr)
                self.assertEqual(list(project.glob(".restore-stage.*")), [])

    def test_remove_defaults_to_retention_and_requires_separate_purge_confirmation(self):
        result = subprocess.run(["bash", "scripts/remove.sh", "--purge-data"], cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--confirm-purge-data", result.stderr)
        self.assertIn("PURGE=false", self.remove)
        self.assertIn("podman volume rm", self.remove)
        self.assertNotIn("podman system prune", self.remove)
        self.assertNotIn("podman volume prune", self.remove)
        self.assertIn('"$PROJECT_NAME.service"', self.remove)

    def _purge_fixture(self, temporary):
        base = Path(temporary)
        project = base / "project"
        fake_bin = base / "bin"
        (project / "scripts").mkdir(parents=True)
        (project / "runtime").mkdir()
        fake_bin.mkdir()
        shutil.copy2(ROOT / "scripts/remove.sh", project / "scripts/remove.sh")
        shutil.copy2(ROOT / "scripts/volume_ownership.py", project / "scripts/volume_ownership.py")
        shutil.copy2(ROOT / "scripts/runtime_config.py", project / "scripts/runtime_config.py")
        shutil.copy2(ROOT / ".env.example", project / ".env")
        log = base / "podman.log"
        inventories = {
            "VOLUME_LIST": base / "volumes",
            "CONTAINER_LIST": base / "containers",
            "NETWORK_LIST": base / "networks",
        }
        inventories["VOLUME_LIST"].write_text(
            "woow_headscale_headscale-data\nwoow_headscale_headscale-run\nwoow_headscale_headplane-data\n"
            "unrelated_headscale-data\nunrelated_headscale-run\nunrelated_headplane-data\n",
            encoding="utf-8",
        )
        inventories["CONTAINER_LIST"].write_text(
            "headscale\nheadplane\nngrok\nunrelated-container\n", encoding="utf-8"
        )
        inventories["NETWORK_LIST"].write_text(
            "woow_headscale_default\nunrelated-network\n", encoding="utf-8"
        )
        (project / "deploy.sh").write_text(
            "#!/bin/bash\n"
            "if [[ ${1:-} == --stop ]]; then\n"
            "  podman container rm headscale\n"
            "  podman container rm headplane\n"
            "  podman container rm ngrok\n"
            "  podman network rm woow_headscale_default\n"
            "fi\n",
            encoding="utf-8",
        )
        (fake_bin / "podman-compose").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        (fake_bin / "podman").write_text(
            "#!/bin/bash\n"
            "echo $* >> \"$TEST_LOG\"\n"
            "remove_from() { local value=$1 file=$2 tmp=${file}.tmp; "
            "grep -Fvx -- \"$value\" \"$file\" > \"$tmp\" || true; mv \"$tmp\" \"$file\"; }\n"
            "if [[ $1 == container && $2 == exists ]]; then "
            "grep -Fxq -- \"$3\" \"$CONTAINER_LIST\"; exit $?; fi\n"
            "if [[ $1 == inspect ]]; then\n"
            "  name=$2; label=${CONTAINER_PROJECT:-woow_headscale}\n"
            "  if [[ $name == headscale ]]; then\n"
            "    data=woow_headscale_headscale-data; run=woow_headscale_headscale-run\n"
            "    [[ ${MOUNT_MISMATCH:-false} != true ]] || data=other_headscale-data\n"
            "    mounts='[{\"Type\":\"volume\",\"Destination\":\"/var/lib/headscale\",\"Name\":\"'\"$data\"'\"},{\"Type\":\"volume\",\"Destination\":\"/var/run/headscale\",\"Name\":\"'\"$run\"'\"}]'\n"
            "  elif [[ $name == headplane ]]; then\n"
            "    mounts='[{\"Type\":\"volume\",\"Destination\":\"/var/lib/headplane\",\"Name\":\"woow_headscale_headplane-data\"}]'\n"
            "  else mounts='[]'; fi\n"
            "  printf '[{\"Config\":{\"Labels\":{\"org.woow-headscale.project\":\"woow-headscale\",\"org.woow-headscale.project-dir\":\"%s\",\"com.docker.compose.project\":\"%s\",\"io.podman.compose.project\":\"%s\"}},\"Mounts\":%s}]' \"$PWD\" \"$label\" \"$label\" \"$mounts\"\n"
            "  exit 0\n"
            "fi\n"
            "if [[ $1 == volume && $2 == ls ]]; then cat \"$VOLUME_LIST\"; exit 0; fi\n"
            "if [[ $1 == volume && $2 == inspect ]]; then\n"
            "  label=${VOLUME_PROJECT:-woow_headscale}\n"
            "  printf '[{\"Name\":\"%s\",\"Labels\":{\"org.woow-headscale.project\":\"woow-headscale\",\"org.woow-headscale.project-dir\":\"%s\",\"com.docker.compose.project\":\"%s\",\"io.podman.compose.project\":\"%s\"}}]' \"$3\" \"$PWD\" \"$label\" \"$label\"; exit 0\n"
            "fi\n"
            "if [[ $1 == volume && $2 == rm ]]; then "
            "grep -Fxq -- \"$3\" \"$VOLUME_LIST\" || exit 1; remove_from \"$3\" \"$VOLUME_LIST\"; exit 0; fi\n"
            "if [[ $1 == container && $2 == rm ]]; then "
            "grep -Fxq -- \"$3\" \"$CONTAINER_LIST\" || exit 1; remove_from \"$3\" \"$CONTAINER_LIST\"; exit 0; fi\n"
            "if [[ $1 == network && $2 == rm ]]; then "
            "grep -Fxq -- \"$3\" \"$NETWORK_LIST\" || exit 1; remove_from \"$3\" \"$NETWORK_LIST\"; exit 0; fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        for executable in (
            project / "scripts/remove.sh", project / "scripts/volume_ownership.py",
            project / "deploy.sh", fake_bin / "podman", fake_bin / "podman-compose",
        ):
            executable.chmod(0o755)
        environment = os.environ.copy()
        environment.update({
            "PATH": str(fake_bin) + os.pathsep + environment["PATH"],
            "TEST_LOG": str(log), "HOME": str(base / "home"),
            **{name: str(path) for name, path in inventories.items()},
        })
        return project, inventories, log, environment

    def test_purge_statefully_removes_only_owned_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, inventories, log, environment = self._purge_fixture(temporary)
            result = subprocess.run(
                [str(project / "scripts/remove.sh"), "--purge-data", "--confirm-purge-data"],
                cwd=project, env=environment, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                inventories["VOLUME_LIST"].read_text(encoding="utf-8").splitlines(),
                ["unrelated_headscale-data", "unrelated_headscale-run", "unrelated_headplane-data"],
            )
            self.assertEqual(
                inventories["CONTAINER_LIST"].read_text(encoding="utf-8").splitlines(),
                ["unrelated-container"],
            )
            self.assertEqual(
                inventories["NETWORK_LIST"].read_text(encoding="utf-8").splitlines(),
                ["unrelated-network"],
            )
            self.assertFalse((project / "runtime").exists())
            removals = [line for line in log.read_text(encoding="utf-8").splitlines() if " rm " in line]
            self.assertNotIn("unrelated_", "\n".join(removals))
            self.assertNotIn("unrelated-", "\n".join(removals))

    def test_purge_ownership_or_mount_mismatch_aborts_before_any_mutation(self):
        for variable, value in (("VOLUME_PROJECT", "somebody-else"), ("MOUNT_MISMATCH", "true")):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as temporary:
                project, inventories, log, environment = self._purge_fixture(temporary)
                before = {
                    name: path.read_text(encoding="utf-8") for name, path in inventories.items()
                }
                environment[variable] = value
                result = subprocess.run(
                    [str(project / "scripts/remove.sh"), "--purge-data", "--confirm-purge-data"],
                    cwd=project, env=environment, text=True, capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(
                    {name: path.read_text(encoding="utf-8") for name, path in inventories.items()},
                    before,
                )
                self.assertTrue((project / "runtime").is_dir())
                self.assertFalse(
                    any(" rm " in line for line in log.read_text(encoding="utf-8").splitlines())
                )

    def test_remove_rejects_retain_and_purge_in_both_orders(self):
        cases = (
            ["--retain-data", "--purge-data", "--confirm-purge-data"],
            ["--purge-data", "--confirm-purge-data", "--retain-data"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    ["bash", "scripts/remove.sh", *arguments],
                    cwd=ROOT, text=True, capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("mutually exclusive", result.stderr)

    def test_docs_preserve_ts2021_nginx_and_lifecycle_contract(self):
        external = (ROOT / "docs/EXTERNAL-ACCESS.md").read_text(encoding="utf-8")
        self.assertIn("default upgrade;", external)
        self.assertIn("'' close;", external)
        self.assertIn("proxy_http_version 1.1", external)
        self.assertIn("proxy_set_header Upgrade", external)
        self.assertIn("proxy_set_header Connection", external)
        self.assertIn("proxy_buffering off", external)
        self.assertIn("does **not** exercise", external)
        for name in ("README.md", "README_zh-TW.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for token in ("NGROK_ENABLED", "NGROK_MODE", "NGROK_DOMAIN", "HEADPLANE_HOST_BIND_ADDR=127.0.0.1", "backup.sh", "restore.sh", "remove.sh", "woow_headscale.service", "default upgrade;"):
                self.assertIn(token, text, name)


if __name__ == "__main__":
    unittest.main()
