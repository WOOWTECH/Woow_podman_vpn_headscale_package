#!/usr/bin/env python3
"""Exercise the protected Headplane pages in a disposable headless browser.

Requires selenium, Firefox and geckodriver. The local-only proxy injects the
Podman secret in memory so it does not enter a browser URL, profile or log.
"""

from __future__ import annotations

import base64
import argparse
from collections.abc import Callable
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import threading
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait


def secret() -> str:
    return subprocess.check_output(
        ["podman", "secret", "inspect", "--showsecret", "--format",
         "{{.SecretData}}", "headscale-entrance-password"], text=True
    ).strip()


def run(inspect_controls: bool = False,
        handler: Callable[[webdriver.Firefox, str, str], None] | None = None
        ) -> list[dict[str, object]]:
    authorization = "Basic " + base64.b64encode(("admin:" + secret()).encode()).decode()
    history: list[tuple[str, int]] = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def handle_proxy(self) -> None:
            upstream = HTTPConnection("127.0.0.1", 23000, timeout=30)
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in {"connection", "authorization"}}
            headers["Authorization"] = authorization
            length = int(self.headers.get("Content-Length", "0"))
            body_in = self.rfile.read(length) if length else None
            try:
                upstream.request(self.command, self.path, body=body_in, headers=headers)
                response = upstream.getresponse()
                body = response.read()
                history.append((self.path, response.status))
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() not in {"connection", "content-length", "transfer-encoding"}:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # browser navigated away from a live event stream
            finally:
                upstream.close()

        do_GET = handle_proxy
        do_POST = handle_proxy
        do_PATCH = handle_proxy
        do_PUT = handle_proxy
        do_DELETE = handle_proxy

    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    options = Options()
    options.add_argument("-headless")
    options.set_preference("network.http.referer.XOriginPolicy", 0)
    driver = None
    results: list[dict[str, object]] = []
    try:
        driver = webdriver.Firefox(options=options, service=Service("/snap/bin/geckodriver"))
        driver.set_page_load_timeout(40)
        for path in ("", "machines", "users", "dns", "acls", "settings",
                     "settings/auth-keys", "settings/restrictions", "settings/agent"):
            route = "/admin/" + path
            driver.get(f"http://127.0.0.1:{server.server_port}{route}")
            WebDriverWait(driver, 15).until(
                lambda browser: browser.execute_script("return document.readyState") == "complete"
            )
            headings = [item.text.strip() for item in driver.find_elements("css selector", "h1")]
            title = driver.title
            statuses = [status for request, status in history if request == route]
            body = driver.find_element("tag name", "body")
            page: dict[str, object] = {"route": route, "status": statuses[-1] if statuses else None,
                            "final_url": urlsplit(driver.current_url).path,
                            "title": title, "headings": headings,
                            "visible": bool(body.is_displayed()),
                            "body_excerpt": body.text[-250:] if path == "acls" else "",
                            "react_errors": driver.execute_script(
                                "return window.__reactRouterContext?.errors || null"
                            ) if path == "acls" else None}
            if inspect_controls:
                page["buttons"] = [
                    {"text": item.text.strip()[:70], "aria": item.get_attribute("aria-label")}
                    for item in driver.find_elements("css selector", "button") if item.is_displayed()
                ]
                page["inputs"] = [
                    {"type": item.get_attribute("type"), "placeholder": item.get_attribute("placeholder")}
                    for item in driver.find_elements("css selector", "input") if item.is_displayed()
                ]
            results.append(page)
            if handler:
                handler(driver, path, f"http://127.0.0.1:{server.server_port}")
    finally:
        if driver:
            driver.quit()
        server.shutdown()
        server.server_close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controls", action="store_true")
    options = parser.parse_args()
    try:
        pages = run(options.controls)
        outcome = {"pages": pages, "passed": all(
            page["status"] in {200, 302} and page["visible"] and page["headings"]
            for page in pages
        )}
    except Exception as error:
        outcome = {"passed": False, "error": type(error).__name__ + ": " + str(error)[:300]}
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0 if outcome["passed"] else 1)
