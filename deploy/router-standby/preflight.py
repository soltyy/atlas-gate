"""Сверка прямого и резервного SSH-портов перед допуском резерва."""

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import sys
import tempfile

MAX_NODE_BYTES = 2048


def node_boot(port, expected_id, token):
    # Ограничение относится только к диагностическому GET на loopback.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/v1/node/identity", headers={"X-Atlas-Token": token})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        data = response.read(MAX_NODE_BYTES + 1)
        if len(data) > MAX_NODE_BYTES:
            raise ValueError("слишком большой ответ /v1/node/identity")
        node = json.loads(data)
        if (not isinstance(node, dict) or node.keys() != {"nodeId", "bootId", "protocolVersion"}
                or node.get("nodeId") != expected_id
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


def save_pins(path, contents):
    # The enrol unit owns this directory. The live selector only reads its pins.
    with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", newline="\n",
                                     dir=path.parent, delete=False) as pending:
        pending_path = Path(pending.name)
        try:
            pending.write(contents)
            pending.flush()
            os.fsync(pending.fileno())
        except BaseException:
            pending.close()
            pending_path.unlink(missing_ok=True)
            raise
    try:
        os.replace(pending_path, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        pending_path.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-pins", type=Path,
                        help="атомарно записать проверенные boot ID для следующего старта HAProxy")
    parser.add_argument("--check-selector", action="store_true",
                        help="прочитать /v1/node/identity через селекторы и сравнить с сохранёнными boot ID")
    args = parser.parse_args()
    if args.write_pins and args.check_selector:
        parser.error("выберите запись boot ID или проверку селектора")
    try:
        if args.check_selector:
            for name in ("CODEX", "CLAUDE"):
                boot = node_boot(int(os.environ[f"{name}_LISTEN_PORT"]),
                                 os.environ[f"{name}_NODE_ID"], os.environ[f"{name}_CHECK_TOKEN"])
                if boot != os.environ[f"{name}_BOOT_ID"]:
                    raise ValueError(f"{name}: селектор вернул другой bootId")
                print(f"{name} selector: HTTP 200, identity OK")
        else:
            verified = verify(os.environ)
            pins = "".join(f"{name}_BOOT_ID={boot}\n" for name, boot in verified.items())
            if args.write_pins:
                save_pins(args.write_pins, pins)
            else:
                sys.stdout.write(pins)
    except (KeyError, OSError, http.client.HTTPException, ValueError) as error:
        sys.exit(f"Сверка не пройдена: {error}")
