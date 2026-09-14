from pathlib import Path
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "systemd/woow_headscale.service"
SERVICE = ROOT / "systemd/woow_headscale_health.service"
TIMER = ROOT / "systemd/woow_headscale_health.timer"


class HealthSchedulerTests(unittest.TestCase):
    def _run_health_unit(self, mode):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            log = temporary / "podman.log"
            state = temporary / "state"
            fake = temporary / "podman"
            fake.write_text(
                "#!/bin/bash\n"
                "printf '%s\\n' \"$*\" >>\"$TEST_LOG\"\n"
                "if [[ $1 == container && $2 == exists ]]; then "
                "[[ $TEST_MODE != absent ]]; exit $?; fi\n"
                "if [[ $1 == container && $2 == inspect ]]; then\n"
                "  [[ $TEST_MODE != stopped ]] || { printf false; exit 0; }\n"
                "  printf true; exit 0\n"
                "fi\n"
                "if [[ $1 == healthcheck && $2 == run ]]; then\n"
                "  [[ $TEST_MODE != failing ]] || exit 9\n"
                "  printf healthy >\"$TEST_STATE\"; exit 0\n"
                "fi\n"
                "exit 2\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "TEST_LOG": str(log), "TEST_STATE": str(state), "TEST_MODE": mode,
            })
            lines = SERVICE.read_text(encoding="utf-8").splitlines()
            conditions = [line.split("=", 1)[1] for line in lines if line.startswith("ExecCondition=")]
            start = next(line.split("=", 1)[1] for line in lines if line.startswith("ExecStart="))

            for condition in conditions:
                command = shlex.split(condition.replace("/usr/bin/podman", str(fake)))
                result = subprocess.run(command, env=environment, text=True, capture_output=True)
                if result.returncode != 0:
                    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
                    return "skipped", calls, state.exists(), result.stdout + result.stderr
            command = shlex.split(start.replace("/usr/bin/podman", str(fake)))
            result = subprocess.run(command, env=environment, text=True, capture_output=True)
            calls = log.read_text(encoding="utf-8").splitlines()
            return ("success" if result.returncode == 0 else "failed"), calls, state.exists(), result.stdout + result.stderr

    def test_health_service_skips_missing_and_stopped_containers(self):
        for mode in ("absent", "stopped"):
            with self.subTest(mode=mode):
                status, calls, state_exists, output = self._run_health_unit(mode)
                self.assertEqual(status, "skipped")
                self.assertFalse(state_exists)
                self.assertFalse(any(call.startswith("healthcheck run") for call in calls))
                self.assertEqual(output, "")

    def test_health_service_runs_live_check_and_propagates_failure(self):
        status, calls, state_exists, output = self._run_health_unit("running")
        self.assertEqual(status, "success")
        self.assertTrue(state_exists, "the manual live check must update container state")
        self.assertEqual(calls[-1], "healthcheck run headscale")
        self.assertEqual(output, "")

        status, calls, state_exists, output = self._run_health_unit("failing")
        self.assertEqual(status, "failed")
        self.assertFalse(state_exists)
        self.assertEqual(calls[-1], "healthcheck run headscale")
        self.assertEqual(output, "")

    def test_timer_is_bounded_persistent_without_stack_ordering_dependency(self):
        timer = TIMER.read_text(encoding="utf-8")
        health_service = SERVICE.read_text(encoding="utf-8")
        self.assertNotIn("After=woow_headscale.service", timer.splitlines())
        self.assertIn("After=woow_headscale.service", health_service.splitlines())
        self.assertIn("OnBootSec=30s", timer)
        self.assertIn("OnUnitActiveSec=30s", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=woow_headscale_health.service", timer)

    def test_rendered_three_unit_set_has_no_systemd_ordering_cycle(self):
        analyzer = shutil.which("systemd-analyze")
        if analyzer is None:
            self.skipTest("systemd-analyze is not installed locally")
        renderer = ROOT / "scripts/render_systemd_unit.py"
        expected_names = [
            "woow_headscale.service",
            "woow_headscale_health.service",
            "woow_headscale_health.timer",
        ]
        sources = sorted((ROOT / "systemd").glob("woow_headscale*"))
        self.assertEqual([source.name for source in sources], expected_names)
        with tempfile.TemporaryDirectory() as temporary:
            targets = []
            for source in sources:
                target = Path(temporary) / source.name
                result = subprocess.run(
                    ["python3", str(renderer), str(source), str(target), str(ROOT)],
                    text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                targets.append(str(target))
            result = subprocess.run(
                [analyzer, "--user", "verify", *targets], text=True, capture_output=True,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertNotIn("ordering cycle", output.lower(), output)

    def test_deploy_drives_live_check_before_verify_and_enables_timer_in_order(self):
        deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        http_success = deploy.index('[[ $headscale_ok == true ]]')
        live_check = deploy.index("capture_command podman healthcheck run headscale", http_success)
        final_verify = deploy.index('"$PROJECT_DIR/scripts/verify.sh"', live_check)
        self.assertLess(http_success, live_check)
        self.assertLess(live_check, final_verify)
        render = deploy.index('for unit_name in "${UNIT_NAMES[@]}"')
        reload = deploy.index("capture_command systemctl --user daemon-reload", render)
        enable = deploy.index("capture_command systemctl --user enable", reload)
        start = deploy.index("capture_command systemctl --user start", enable)
        self.assertLess(render, reload)
        self.assertLess(reload, enable)
        self.assertLess(enable, start)
        self.assertLess(final_verify, start)
        self.assertIn('"$COMPOSE_PROJECT_NAME.service" "${COMPOSE_PROJECT_NAME}_health.timer"', deploy[enable:start])
        start_command = deploy[start:deploy.index("printf 'Deployment verified", start)]
        self.assertIn('"${COMPOSE_PROJECT_NAME}_health.timer"', start_command)
        self.assertNotIn('"${COMPOSE_PROJECT_NAME}_health.service"', start_command)

    def test_fake_main_restart_rearms_only_timer_after_execstart(self):
        deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        main = MAIN.read_text(encoding="utf-8")
        self.assertIn("After=woow_headscale.service", SERVICE.read_text(encoding="utf-8").splitlines())
        self.assertNotIn("After=woow_headscale.service", TIMER.read_text(encoding="utf-8").splitlines())

        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            fake_bin = temporary / "bin"
            fake_bin.mkdir()
            state = temporary / "timer.state"
            log = temporary / "systemctl.log"
            state.write_text("active\n", encoding="utf-8")
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/bin/bash\n"
                "printf '%s\\n' \"$*\" >>\"$TEST_LOG\"\n"
                "case \" $* \" in\n"
                "  *' stop '*'woow_headscale_health.timer'*) printf inactive >\"$TEST_STATE\" ;;\n"
                "  *' start '*'woow_headscale_health.timer'*) printf active >\"$TEST_STATE\" ;;\n"
                "esac\n"
                "[[ \" $* \" != *' start '*'woow_headscale_health.service'* ]]\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "PATH": str(fake_bin) + os.pathsep + environment["PATH"],
                "TEST_LOG": str(log), "TEST_STATE": str(state),
            })

            stop = next(line.split("=", 1)[1] for line in main.splitlines()
                        if line.startswith("ExecStop=-/usr/bin/systemctl"))
            stop = stop.removeprefix("-").replace("/usr/bin/systemctl", str(systemctl))
            result = subprocess.run(shlex.split(stop), env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(state.read_text(encoding="utf-8"), "inactive")

            scheduler = deploy[deploy.index("# ExecStop disables the scheduler"):]
            scheduler = scheduler[:scheduler.index("printf 'Deployment verified")]
            harness = temporary / "execstart-tail.sh"
            harness.write_text(
                "#!/bin/bash\nset -e\nCOMPOSE_PROJECT_NAME=woow_headscale\n"
                "capture_command() { \"$@\"; }\nfail() { echo \"$*\" >&2; exit 1; }\n"
                + scheduler,
                encoding="utf-8",
            )
            harness.chmod(0o755)
            result = subprocess.run([str(harness)], env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(state.read_text(encoding="utf-8"), "active")
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("stop woow_headscale_health.timer woow_headscale_health.service" in call for call in calls))
            self.assertTrue(any("start woow_headscale_health.timer" in call for call in calls))
            self.assertFalse(any("start woow_headscale_health.service" in call for call in calls))

    def test_verify_runs_live_check_before_reading_health_state(self):
        verify = (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")
        http = verify.index('if curl --fail --silent --max-time 5 "$HEADSCALE_PROBE/health"')
        live = verify.index("podman healthcheck run headscale", http)
        state = verify.index("container_health_status headscale", live)
        self.assertLess(http, live)
        self.assertLess(live, state)
        self.assertIn("== healthy", verify[state:state + 100])

    def test_retain_removal_disables_and_removes_health_units_before_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            fake_bin = base / "bin"
            unit_dir = base / "home/.config/systemd/user"
            (project / "scripts").mkdir(parents=True)
            (project / "systemd").mkdir()
            fake_bin.mkdir()
            unit_dir.mkdir(parents=True)
            for name in ("remove.sh", "runtime_config.py", "volume_ownership.py"):
                source = ROOT / "scripts" / name
                shutil.copy2(source, project / "scripts" / name)
            shutil.copy2(ROOT / ".env.example", project / ".env")
            for source in (SERVICE, TIMER):
                shutil.copy2(source, project / "systemd" / source.name)
                shutil.copy2(source, unit_dir / source.name)
            main = unit_dir / "woow_headscale.service"
            main.write_text(f"ExecStart={project}/deploy.sh --from-systemd\n", encoding="utf-8")
            log = base / "calls.log"
            (project / "deploy.sh").write_text(
                "#!/bin/bash\nprintf 'deploy %s\\n' \"$*\" >>\"$TEST_LOG\"\n",
                encoding="utf-8",
            )
            (fake_bin / "systemctl").write_text(
                "#!/bin/bash\nprintf 'systemctl %s\\n' \"$*\" >>\"$TEST_LOG\"\n",
                encoding="utf-8",
            )
            (fake_bin / "podman").write_text(
                "#!/bin/bash\n"
                "if [[ $1 == container && $2 == exists ]]; then exit 1; fi\nexit 1\n",
                encoding="utf-8",
            )
            (fake_bin / "podman-compose").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            for executable in (
                project / "scripts/remove.sh", project / "deploy.sh",
                fake_bin / "systemctl", fake_bin / "podman", fake_bin / "podman-compose",
            ):
                executable.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "PATH": str(fake_bin) + os.pathsep + environment["PATH"],
                "HOME": str(base / "home"), "TEST_LOG": str(log),
            })
            result = subprocess.run(
                [str(project / "scripts/remove.sh"), "--retain-data"],
                cwd=project, env=environment, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            health_disable = next(i for i, call in enumerate(calls) if "health.service" in call)
            stack_stop = calls.index("deploy --stop")
            self.assertLess(health_disable, stack_stop)
            self.assertIn("woow_headscale_health.timer", calls[health_disable])
            self.assertFalse((unit_dir / SERVICE.name).exists())
            self.assertFalse((unit_dir / TIMER.name).exists())
            self.assertFalse(main.exists())


if __name__ == "__main__":
    unittest.main()
