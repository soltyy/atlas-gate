"""Сверка прямого и резервного SSH-портов перед допуском резерва."""

import http.client
import json
import os
import re
import sys

MAX_NODE_BYTES = 32 * 1024


def node_boot(port, expected_id, token):
    # Ограничение относится только к диагностическому GET на loopback.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/v1/node", headers={"X-Atlas-Token": token})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        data = response.read(MAX_NODE_BYTES + 1)
        if len(data) > MAX_NODE_BYTES:
            raise ValueError("слишком большой ответ /v1/node")
        node = json.loads(data)
        if (not isinstance(node, dict) or node.get("nodeId") != expected_id
                or type(node.get("protocolVersion")) is not int or node["protocolVersion"] != 1):
            raise ValueError("не совпали nodeId или protocolVersion")
        boot = node.get("bootId")
        if not isinstance(boot, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", boot):
            raise ValueError("неподдерживаемый bootId")
        return boot
    finally:
        conn.close()


def verify(env):
    used_ports = set()
    used_ids = set()
    boots = {}
    for name in ("CODEX", "CLAUDE"):
        ports = {}
        for role in ("DIRECT", "STANDBY", "LISTEN"):
            port = int(env[f"{name}_{role}_PORT"])
            if not 1 <= port <= 65535 or port in used_ports:
                raise ValueError(f"{name}_{role}_PORT вне диапазона или используется дважды")
            used_ports.add(port)
            ports[role] = port
        expected_id = env[f"{name}_NODE_ID"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", expected_id) or expected_id in used_ids:
            raise ValueError(f"недопустимый или повторный {name}_NODE_ID")
        used_ids.add(expected_id)
        token = env[f"{name}_CHECK_TOKEN"]
        if not token or any(not 33 <= ord(c) <= 126 for c in token):
            raise ValueError(f"некорректный {name}_CHECK_TOKEN")

        # Вторая сверка direct обнаруживает перезапуск узла между запросами.
        for role in ("DIRECT", "STANDBY", "DIRECT"):
            try:
                boot = node_boot(ports[role], expected_id, token)
            except (OSError, http.client.HTTPException, ValueError) as exc:
                raise ValueError(f"{name} {role} ({ports[role]}): {exc}") from None
            if name in boots and boots[name] != boot:
                raise ValueError(f"{name}: bootId различается между путями или изменился при сверке")
            boots[name] = boot
    return boots


if __name__ == "__main__":
    try:
        verified = verify(os.environ)
    except (KeyError, ValueError) as error:
        sys.exit(f"Сверка не пройдена: {error}")
    for router_name, boot_id in verified.items():
        print(f"{router_name}_BOOT_ID={boot_id}")
