#!/usr/bin/env python3
"""Catch Headplane's second-request ACL SSR crash through the protected entrance."""

import base64
import subprocess
import sys
import time
import urllib.error
import urllib.request


password = subprocess.check_output(
    ["podman", "secret", "inspect", "--showsecret", "--format",
     "{{.SecretData}}", "headscale-entrance-password"], text=True
).strip()
header = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()
url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:23000/admin/acls"
request = urllib.request.Request(url, headers={"Authorization": header})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
statuses = []
for _ in range(5):
    try:
        with opener.open(request, timeout=15) as response:
            response.read()
            statuses.append(response.status)
    except urllib.error.HTTPError as error:
        statuses.append(error.code)
    time.sleep(0.35)
print("ACL SSR statuses: " + ", ".join(map(str, statuses)))
sys.exit(0 if statuses == [200] * 5 else 1)
